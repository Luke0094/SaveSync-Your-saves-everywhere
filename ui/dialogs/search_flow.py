"""
SaveSync - Web-search / enrichment flow for the Add-Edit Game dialog.

SearchFlowMixin hosts the whole search machine extracted verbatim from
AddGameDialog: tiered web search + result handling, the candidate carousel
hand-off, the authoritative-candidate apply (init / enrich / soft-promote),
the same-tier fill-only enrichment offer with its chip merge model, source
bookkeeping (applied sources + primary reachability), and direct
fetch-from-URL. AddGameDialog provides the widgets and state the methods
use (self._name_input, self._search_btn, self._last_search_candidates, ...);
the mixin MUST come first in the MRO.
"""
import logging
import threading
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QVBoxLayout)

from i18n import t
from ui.helpers import finalize_adaptive_dialog_size, scaled
from ui.styles.theme import palette
from ui.dialogs.search_enrichment import (CandidatePreviewDialog,
                                          EnrichmentMergeDialog,
                                          _inspect_url)

logger = logging.getLogger(__name__)


class SearchFlowMixin:
    def _capture_session_initial_image(self) -> None:
        """Snapshot the image on the form before this dialog session's
        FIRST download, once only — every entry point that can trigger a
        download (web search, direct URL fetch) must call this before
        doing so. _cleanup_session_icon_dirs (add_game_dialog.py) uses the
        snapshot to keep that one file and discard everything else this
        session put in the same folder when the dialog closes unsaved.
        Missing this call from any one entry point left the flag/path
        unset, and the cleanup's "keep the original" check then compared
        every file against None — which never matches a real path — wiping
        the ENTIRE icon folder, original included, instead of just the
        session's own downloads."""
        if not self._session_image_captured:
            self._session_initial_image_path = self._image_path
            self._session_image_captured = True

    def _web_search(self, enable_web_fallback: bool = False,
                    skip_primary_apis: bool = False,
                    enable_targeted_fallback: Optional[bool] = None,
                    enable_generic_fallback: Optional[bool] = None,
                    extra_folder_hint: str = '',
                    status_msg: str = '',
                    skip_sources: list[str] | None = None):
        """Search for game info from primary APIs (Steam, PCGamingWiki, VNDB).

        *extra_folder_hint* is an additional folder/DL-code hint to pass to the
        search even when it cannot be derived from the current name field (e.g.
        after a result was applied that changed the name away from the original).

        *status_msg* overrides the default "Searching…" status label text.
        """
        game_name = self._name_edit.text().strip()
        if not game_name:
            self._status_lbl.setText(t('add_game.field_required', field=t('add_game.name')))
            self._status_lbl.setStyleSheet(f"color:{palette('error')};")
            return

        # One bg op per card — URL→exe / detect must not race web search.
        # Tier cascades (api→targeted→generic) clear ``_web_search_active``
        # before re-entering; a same-card concurrent start is still blocked.
        if self._has_shelvable_work():
            return

        exe_path = self._exe_edit.text().strip()

        appid = None
        from core.resolvers import is_launcher_url, get_appid_from_url
        if is_launcher_url(exe_path):
            appid = get_appid_from_url(exe_path)

        fs = scaled(12, self)
        self._status_lbl.setText(status_msg or t('add_game.searching'))
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:{fs}px;")
        self._search_progress.setVisible(True)
        self._web_search_active = True
        self._pending_search_payload = None
        self._bg_work_kind = "search"
        self._emit_bg_status("running")

        # A new search invalidates any candidate popup from the previous one
        # (nothing to do here now — CandidatePreviewDialog is transient and
        # already closed by the time a new search can be triggered).

        # Gate conflicting actions; form fields / Cancel / manual paths stay usable.
        # Save is locked via _sync_bg_action_gates while this search runs.
        self._sync_bg_action_gates()

        # Store current state to check for overwrite
        self._original_name = self._name_edit.text().strip()
        self._original_desc = self._desc_edit.toPlainText().strip()
        self._original_image_path = self._image_path
        self._original_image_url = self._image_path_to_url.get(self._image_path) if self._image_path else None

        self._capture_session_initial_image()

        # Resolve fallback flags and track search phase
        if enable_targeted_fallback is None and enable_generic_fallback is None:
            # Legacy: use enable_web_fallback to decide
            _use_targeted = enable_web_fallback
            _use_generic = enable_web_fallback
        else:
            _use_targeted = bool(enable_targeted_fallback)
            _use_generic = bool(enable_generic_fallback)
        self._current_search_phase = 'generic' if _use_generic and skip_primary_apis else \
                                     'targeted' if _use_targeted and skip_primary_apis else \
                                     'api'
        import logging as _log2
        _log2.getLogger(__name__).info(
            f"_web_search: phase={self._current_search_phase!r} "
            f"skip_primary_apis={skip_primary_apis} "
            f"use_targeted={_use_targeted} use_generic={_use_generic}"
        )
        self._cancel_event.clear()

        # Collect exe stem and folder name as additional search hints.
        # Priority: exe's parent directory tree over game name, walking up
        # to find the most descriptive folder name.
        # Reject any name that matches a generic exe stem.
        from core.save_detector import GENERIC_EXE_STEMS as _GENERIC_STEMS
        from core.save_detector import _CONTAINER_DIR_NAMES as _CONTAINER_STEMS
        import re as _hint_re
        _exe_path = self._exe_edit.text().strip()
        _folder_name = ""
        try:
            # If game name contains a DLsite product code (RJ/RE/VJ), use it
            # as the folder hint — it's far more precise than folder names.
            _dl_code_match = _hint_re.search(r'(RJ|RE|VJ)(\d{4,10})', game_name, _hint_re.IGNORECASE)
            if _dl_code_match:
                _folder_name = _dl_code_match.group(0).upper()
            elif _exe_path:
                _parent = Path(_exe_path).parent
                # Walk up from the exe's parent directory, preferring the
                # longest non-generic name as the search hint. The walk is
                # bounded by _CONTAINER_DIR_NAMES (steamapps, program files,
                # appdata, users, downloads…) — the same launcher/OS boundary
                # derive_display_name() uses — so a system/launcher path
                # segment (e.g. "Program Files (x86)") can never outlast a
                # short real game name and win the "longest candidate" pick.
                _candidates: list[tuple[str, int]] = []
                _cur = _parent
                while _cur != _cur.parent:
                    _n = _cur.name
                    _nl = _n.lower() if _n else ''
                    if _nl in _CONTAINER_STEMS:
                        # Launcher/OS boundary reached — stop climbing here;
                        # this segment (and anything above it) is never a
                        # valid search hint.
                        break
                    if _n and _nl not in _GENERIC_STEMS:
                        _candidates.append((_n, len(_n)))
                    _cur = _cur.parent
                if _candidates:
                    _candidates.sort(key=lambda x: -x[1])
                    _folder_name = _candidates[0][0]
        except Exception:
            pass

        # If no DL code / folder found from current name, use the caller-provided
        # hint (e.g. enrichment preserving an RJ code from the original search).
        if not _folder_name and extra_folder_hint:
            _folder_name = extra_folder_hint

        # Folder may embed a product code plus the unclean title/version
        # ("RJ01234567] My Game v1.01"). Keep the FULL string — tier 2
        # extracts the code itself; tier 3 needs the title+version beside it.
        # Only collapse to the bare code when the folder is nothing else.
        if _folder_name:
            _folder_dl = _hint_re.search(
                r'(RJ|RE|VJ)(\d{4,10})', _folder_name, _hint_re.IGNORECASE)
            if _folder_dl:
                _code = (_folder_dl.group(1) + _folder_dl.group(2)).upper()
                _rest = _hint_re.sub(
                    r'(RJ|RE|VJ)\d{4,10}', '', _folder_name,
                    flags=_hint_re.IGNORECASE,
                )
                _rest = _rest.strip(' [](){}-_–—|.,;')
                if not _rest:
                    _folder_name = _code

        # If the game name was renamed after the last accepted result, the
        # original accepted name (stored in the fingerprint) can help the
        # search find the right game even after a rename.
        if not _folder_name:
            _fp_name = (getattr(self, '_enrichment_source_fingerprint', {}) or {}).get('name', '')
            if _fp_name and _fp_name.lower() != game_name.lower():
                _folder_name = _fp_name

        # Always persist so the enrichment chain can pass it forward.
        self._last_search_folder_hint = _folder_name

        def do_search():
            import logging
            _log = logging.getLogger(__name__)
            error = None
            result = None
            try:
                from core.game_api import search_game_info_multi
                # Every relevant title, best first — the handler shows a
                # picker when more than one distinct title comes back.
                result = search_game_info_multi(
                    game_name,
                    appid if appid else None,
                    enable_web_fallback=enable_web_fallback,
                    exe_path=_exe_path,
                    folder_name=_folder_name,
                    skip_primary_apis=skip_primary_apis,
                    enable_targeted_fallback=_use_targeted,
                    enable_generic_fallback=_use_generic,
                    skip_api_sources=list(skip_sources) if skip_sources else [],
                    skip_targeted_sources=list(skip_sources) if skip_sources else [],
                )
            except Exception as e:
                error = e
                _log.error(f"Web search error: {e}", exc_info=True)
            try:
                self.search_finished.emit(result, error)
            except RuntimeError:
                pass   # dialog destroyed while this background search ran

        threading.Thread(target=do_search, daemon=True).start()

    def _on_search_finished(self, result, error):
        """Handle search completion."""
        self._search_progress.setVisible(False)
        if self._cancel_event.is_set():
            self._web_search_active = False
            self._sync_bg_action_gates()
            self._emit_bg_status("failed")
            try:
                self.background_idle.emit()
            except Exception:
                pass
            return
        # Shelved (✕ while searching): keep the dialog alive, stash the
        # outcome, and let the sidebar reopen apply it — same as the batch
        # web-search panel. Do not run modal follow-ups on a hidden window.
        if not self.isVisible():
            self._web_search_active = False
            self._pending_search_payload = (result, error)
            self._sync_bg_action_gates()
            self._emit_bg_status("failed" if error else "done")
            try:
                self.background_idle.emit()
            except Exception:
                pass
            return

        # The worker emits a best-first list of candidates (may be empty);
        # tolerate a single GameInfo/None for any legacy emitter.
        results = [r for r in (result if isinstance(result, list) else [result]) if r]

        fs = scaled(12, self)
        self._status_lbl.setStyleSheet(f"color:{palette('text_secondary')};font-size:{fs}px;")

        if error:
            self._web_search_active = False
            self._sync_bg_action_gates()
            self._status_lbl.setText(t('add_game.search_error', error=str(error)[:50]))
            fs = scaled(12, self)
            self._status_lbl.setStyleSheet(f"color:{palette('error')};font-size:{fs}px;")
            self._emit_bg_status("failed")
            return

        _current_phase = getattr(self, '_current_search_phase', 'api')
        import logging as _log3
        _log3.getLogger(__name__).info(
            f"_on_search_finished: phase={_current_phase!r} "
            f"results={[r.name for r in results]!r}"
        )

        if not results:
            fs = scaled(12, self)
            self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:{fs}px;")
            self._sync_bg_action_gates()
            _hint_fwd = getattr(self, '_last_search_folder_hint', '')
            # When every web engine is in a rate-limit cooldown, "not found"
            # is misleading — nothing was actually searched. Tell the user
            # what happened and when it's worth retrying.
            _rate_limited = ''
            if _current_phase in ('targeted', 'generic'):
                try:
                    from core.game_api import engines_blocked_status
                    _blk, _tot, _mins = engines_blocked_status()
                    if _blk >= _tot:
                        _rate_limited = t('add_game.engines_rate_limited', min=_mins)
                except Exception:
                    pass
            from ui.modal_helpers import question_window_modal as _qwm_phase
            if _current_phase == 'api':
                self._status_lbl.setText(
                    t('add_game.enrichment_step_status',
                      source=t('add_game.enrich_api'), status=t('add_game.search_not_found'))
                )
                _r = _qwm_phase(
                    self, t('add_game.confirm_phase_title'),
                    t('add_game.confirm_phase_msg', phase=t('add_game.phase_targeted')),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    button_texts={QMessageBox.StandardButton.Yes: t('common.yes'),
                                  QMessageBox.StandardButton.No:  t('common.no')}
                )
                if _r == QMessageBox.StandardButton.Yes:
                    # Clear so the mutex lets this tier cascade re-enter.
                    self._web_search_active = False
                    self._web_search(skip_primary_apis=True, enable_targeted_fallback=True,
                                     extra_folder_hint=_hint_fwd)
                else:
                    self._web_search_active = False
                    self._emit_bg_status("failed")
            elif _current_phase == 'targeted':
                self._status_lbl.setText(
                    _rate_limited or
                    t('add_game.enrichment_step_status',
                      source=t('add_game.enrich_targeted'), status=t('add_game.search_not_found'))
                )
                _r = _qwm_phase(
                    self, t('add_game.confirm_phase_title'),
                    t('add_game.confirm_phase_msg', phase=t('add_game.phase_generic')),
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    button_texts={QMessageBox.StandardButton.Yes: t('common.yes'),
                                  QMessageBox.StandardButton.No:  t('common.no')}
                )
                if _r == QMessageBox.StandardButton.Yes:
                    self._web_search_active = False
                    self._web_search(skip_primary_apis=True, enable_generic_fallback=True,
                                     extra_folder_hint=_hint_fwd)
                else:
                    self._web_search_active = False
                    self._emit_bg_status("failed")
            else:
                self._web_search_active = False
                self._emit_bg_status("failed")
                self._status_lbl.setText(
                    _rate_limited or t('add_game.search_not_found')
                )
            return

        # One distinct title or several: always review through the same
        # candidate-preview popup (‹ › browse when there's more than one,
        # arrows simply disabled for a single result) instead of silently
        # taking the first one. Near-duplicate sources that only differ in
        # description are dropped on purpose (see _dedupe_except_description).
        self._web_search_active = False
        self._emit_bg_status("done")
        self._show_search_candidates(results)

    def _source_label(self, raw_source: str) -> str:
        """Map an internal source id ('steam', 'web', 'itch+web'…) to its
        human label, preserving the '+ web' enrichment suffix."""
        from core.game_sources.common import source_label
        return source_label(raw_source)

    # ── Web-search candidate preview (single title or several) ───────────────

    def _show_search_candidates(self, results: list, *, merge_pool: list | None = None):
        """Open the unified candidate-preview popup for a search outcome —
        one distinct title or several; both go through the SAME dialog now
        (CandidatePreviewDialog), so reviewing a result always shows the
        same concrete detail (image, description, developer, year, tags,
        source) whether there's one candidate or many, with ‹ › to browse
        when there's more than one. The form is only touched once the user
        confirms — browsing and rejecting are both preview-only.

        After confirm, same-tier peers open the chip merge dialog. Back there
        restores the form snapshot and reopens this carousel so the user can
        pick another candidate without being stuck.

        *merge_pool*, when given, is what same-tier peer lookup searches
        instead of *results* — used by the direct-URL fetch, which shows
        only the one fetched page in the carousel but must still be able to
        find a same-tier peer among candidates an earlier search already
        found (otherwise that earlier batch is silently forgotten and the
        fetched page can never offer a chip merge against it).

        Primary-page reachability (soft-promote) is probed on a worker
        thread — never on the GUI thread — and the carousel is refreshed
        when that answer arrives.
        """
        self._sync_bg_action_gates()
        # Full set kept for same-tier merge peers; carousel only shows
        # candidates that would actually change something (or soft-promote
        # a dead primary). Re-offering Steam after a Steam save + VNDB
        # enrich with nothing new is exactly what this filters out.
        self._last_search_candidates = (
            list(merge_pool) if merge_pool is not None else list(results))
        self._pending_reachability_results = list(results)
        self._candidate_show_deferred = False
        self._active_candidate_dialog = None
        self._invalidate_primary_reachability()
        probing = self._start_primary_reachability_probe()

        _diffs = [(r, self._compute_candidate_diff(r)) for r in results]
        for _r, _d in _diffs:
            logger.info(
                f"_show_search_candidates: {getattr(_r, 'source', '')!r}/"
                f"{getattr(_r, 'name', '')!r} has_changes={_d.get('has_changes')} "
                f"same_origin={_d.get('same_origin')} "
                f"same_origin_no_diff={_d.get('same_origin_no_diff')} "
                f"already_applied={_d.get('already_applied')} "
                f"name_change={_d.get('name_change')} "
                f"has_material={_d.get('has_material')} "
                f"fields={list((_d.get('fields') or {}).keys())} "
                f"new_tags={_d.get('new_tags')!r} new_urls={_d.get('new_urls')!r} "
                f"new_reviews_n={len(_d.get('new_reviews') or [])} "
                f"promote_primary={_d.get('promote_primary')} "
                # Raw review fields straight off the source, before any
                # "already saved" filtering — answers "did the API even
                # return a review" independent of whether it counted as new.
                f"raw_rating={getattr(_r, 'rating', None)!r} "
                f"raw_review_text={(getattr(_r, 'review_text', '') or '')[:40]!r} "
                f"raw_vote_count={getattr(_r, 'vote_count', None)!r} "
                f"raw_reviews_n={len(getattr(_r, 'reviews', None) or [])} "
                # Settles "year missing from preview" the same way: was a
                # year even extracted from the source's raw release_date,
                # and did it survive to result_year (diff computation) —
                # independent of whether fields['year'] ended up populated
                # (which additionally requires it to differ from the form).
                f"raw_release_date={getattr(_r, 'release_date', '') or ''!r} "
                f"result_year={_d.get('result_year')!r} "
                f"current_year={self._year_edit.text().strip()!r} "
                # Name is compared with a plain != (see name_change in
                # _compute_candidate_diff) — printing both sides settles
                # "the candidate has a different name but nothing happened"
                # without guessing whether the two strings actually differ
                # (whitespace/decoding could make them equal despite looking
                # different, or genuinely differ despite looking the same).
                f"current_name={self._name_edit.text().strip()!r}")
        useful = [r for r, d in _diffs if d.get('has_changes')]
        # Sources that match another candidate on everything except
        # description are skipped on purpose — only one of them is proposed.
        useful = self._dedupe_except_description(useful)
        if not useful:
            if probing:
                # Only promote_primary could unlock a candidate — wait for
                # the probe without freezing the UI (same Signal pattern as
                # search_finished / _UrlFetchDialog._fetch_done).
                self._candidate_show_deferred = True
                self._status_lbl.setText(t('add_game.searching'))
                self._status_lbl.setStyleSheet(
                    f"color:{palette('text_secondary')};font-size:{scaled(12, self)}px;")
                return
            # Nothing this tier found was worth showing (has_changes=False
            # for all of them — e.g. VNDB re-confirms data already saved).
            # That's a verdict on THIS tier's data being redundant, not on
            # whether the game has more to find elsewhere — cascade exactly
            # like an explicit reject would, instead of just stopping here
            # and leaving a later tier (which might have something this one
            # doesn't, e.g. a review count) never tried.
            self._candidates_rejected()
            return
        self._open_candidate_carousel(useful)

    def _open_candidate_carousel(self, useful: list):
        """Modal carousel loop for an already-filtered useful list."""
        n = len(useful)
        self._status_lbl.setText(
            t('add_game.candidates_found', n=n) if n > 1
            else t('add_game.candidate_found_single')
        )
        fs = scaled(12, self)
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:{fs}px;")
        while True:
            dlg = CandidatePreviewDialog(
                useful, self._compute_candidate_diff, self,
                extra_note=t('add_game.enrich_note') if n > 1 else '',
            )
            self._active_candidate_dialog = dlg
            try:
                if dlg.exec() != QDialog.DialogCode.Accepted or dlg.selected is None:
                    if dlg.explicitly_declined:
                        self._candidates_rejected()
                    else:
                        # Closed via the window's own X, not the No button —
                        # stop looking entirely rather than cascading to the
                        # next search tier the way an explicit No does.
                        self._candidate_selection_cancelled()
                    return
                snap = self._capture_search_form()
                if not self._process_search_result(dlg.selected, offer_enrichment=False):
                    return
                if self._run_same_tier_merge(dlg.selected, pre_confirm_name=snap.get('name', '')):
                    self._restore_search_form(snap)
                    useful = self._dedupe_except_description([
                        r for r in (self._last_search_candidates or [])
                        if self._compute_candidate_diff(r).get('has_changes')
                    ])
                    if not useful:
                        return
                    n = len(useful)
                    continue
                # Code-hint DLsite URLs survive even when that candidate was
                # not the one confirmed (and even if its merge URL chip was
                # left unchecked) — the product code guarantees the work.
                self._retain_hint_coded_dlsite_urls()
                return
            finally:
                self._active_candidate_dialog = None

    def _retain_hint_coded_dlsite_urls(self):
        """Keep DLsite product URLs that match a search-hint product code.

        A folder/name RJ/RE/VJ code guarantees the same work: even if the
        user rejected the DLsite candidate (wrong-looking title, region
        lock, …) the store link stays. Keyword-only DLsite hits without a
        matching code hint are discarded with the reject — never
        pre-approved onto another source.
        """
        from core.manual_paths import product_codes
        import re as _re_dl
        codes: set[str] = set()
        for bit in (
            getattr(self, '_last_search_folder_hint', '') or '',
            self._name_edit.text() if hasattr(self, '_name_edit') else '',
        ):
            codes |= product_codes(bit)
        if not codes:
            return
        urls = list(getattr(self, '_store_urls', None) or [])
        added = False
        for r in getattr(self, '_last_search_candidates', None) or []:
            src = (getattr(r, 'source', '') or '').split('+')[0].lower()
            if src != 'dlsite':
                continue
            for u in self._result_site_urls(r):
                m = _re_dl.search(
                    r'product_id/((?:RJ|RE|VJ)\d{4,10})', u or '',
                    _re_dl.IGNORECASE,
                )
                if not m or m.group(1).upper() not in codes:
                    continue
                if u not in urls:
                    urls.append(u)
                    added = True
        if added:
            self._store_urls = urls
            if hasattr(self, '_rebuild_url_chips'):
                self._rebuild_url_chips()
            logger.info(
                "Retained DLsite URL(s) matching hint product code(s) "
                f"{sorted(codes)}"
            )

    def _candidate_selection_cancelled(self):
        """The candidate picker was closed via its own window X, not No:
        stop looking entirely instead of cascading to the next search tier.
        No's cascade is an explicit "try somewhere else"; closing the window
        is "never mind" and used to be silently treated the same way."""
        self._retain_hint_coded_dlsite_urls()
        self._web_search_active = False
        self._sync_bg_action_gates()
        self._emit_bg_status("failed")
        self._status_lbl.setText(t('add_game.candidate_selection_cancelled'))
        self._status_lbl.setStyleSheet(
            f"color:{palette('text_secondary')};font-size:{scaled(12, self)}px;")

    def _candidates_rejected(self):
        """No confirmed (the No button): proceed straight to the next
        search tier, exactly like declining a single result — no extra "do
        you want to try the next tier?" prompt, the user already said no
        once by rejecting. Mirrors the decline branch in
        _process_search_result. Closing the popup's own window instead of
        clicking No goes to _candidate_selection_cancelled, not here."""
        # Product-code DLsite links outlive a full reject; keyword-only ones
        # do not (see _retain_hint_coded_dlsite_urls).
        self._retain_hint_coded_dlsite_urls()
        _current_phase = getattr(self, '_current_search_phase', 'api')
        _hint_fwd = getattr(self, '_last_search_folder_hint', '')
        self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:{scaled(12, self)}px;")
        self._sync_bg_action_gates()
        if _current_phase == 'api':
            self._status_lbl.setText(
                t('add_game.enrichment_step_status',
                  source=t('add_game.enrich_api'), status=t('add_game.search_not_found'))
            )
            self._web_search_active = False
            self._web_search(skip_primary_apis=True, enable_targeted_fallback=True,
                             extra_folder_hint=_hint_fwd)
        elif _current_phase == 'targeted':
            self._status_lbl.setText(
                t('add_game.enrichment_step_status',
                  source=t('add_game.enrich_targeted'), status=t('add_game.search_not_found'))
            )
            self._web_search_active = False
            self._web_search(skip_primary_apis=True, enable_generic_fallback=True,
                             extra_folder_hint=_hint_fwd)
        else:
            self._web_search_active = False
            self._status_lbl.setText(t('add_game.search_not_found'))
            self._emit_bg_status("failed")

    def _process_search_result(self, result, offer_enrichment: bool = True) -> bool:
        """Apply ONE confirmed search result — either the only candidate
        found, or the one the user confirmed in the candidate-preview
        popup (CandidatePreviewDialog).

        Returns True when the form was updated. When *offer_enrichment* is
        True (default), same-tier peers open the chip merge dialog; the
        candidate carousel drives that itself with offer_enrichment=False
        so Back can restore a snapshot first.
        """
        _raw_source = getattr(result, 'source', '') or ''

        diff = self._compute_candidate_diff(result)

        # ── Nothing at all to apply → say so and STOP ─────────────────────
        # Confirming a candidate is never a request to keep searching: the
        # popup's own hint promises "Yes applies this result and stops here"
        # (No is what tries another source). This used to silently fire a
        # fresh search — same tier minus this source, or the next tier —
        # which read as "I pressed Yes and it went looking again, then told
        # me nothing was found".
        #
        # Tested on has_changes rather than the narrower has_enrich /
        # same_origin_no_diff pair those two branches used: a same-source
        # candidate can still carry a new cover, tag or store link, and
        # those were being discarded together with the duplicate text.
        if not diff['has_changes']:
            fs = scaled(12, self)
            self._status_lbl.setStyleSheet(f"color:{palette('text_secondary')};font-size:{fs}px;")
            self._sync_bg_action_gates()
            self._status_lbl.setText(t('add_game.candidate_no_changes'))
            return False

        # ── Apply — the user already confirmed this exact candidate ───────
        entry = getattr(self, '_entry', None)
        is_from_bulk_auto = bool(
            entry and (
                getattr(entry, 'auto_added', False)
                or getattr(entry, 'requires_confirmation', False)
                or (getattr(entry, 'cloud_metadata', {}) or {}).get('auto_enriched', False)
            )
        )
        if not diff['has_existing']:
            self._apply_result_init(result)
        elif is_from_bulk_auto:
            # Game was added/enriched automatically in bulk — replace primary info to fix potential false positives
            self._apply_result_overwrite(result)
            if entry:
                entry.auto_added = False
                if isinstance(getattr(entry, 'cloud_metadata', None), dict):
                    entry.cloud_metadata['auto_enriched'] = False
        else:
            # User-managed game — non-destructive enrichment (only fill missing fields)
            self._apply_result_init(result)
        self._store_result_fingerprint(
            _raw_source, result, diff['result_year'],
            as_primary=bool(
                not diff['has_existing']
                or is_from_bulk_auto
                or diff.get('promote_primary')
                or diff.get('same_origin')
            ),
        )

        self._status_lbl.setText(t('add_game.data_saved'))
        fs = scaled(12, self)
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:{fs}px;")
        self._sync_bg_action_gates()

        if offer_enrichment:
            _peers = [r for r in (getattr(self, '_last_search_candidates', None) or [])
                      if r is not result]
            if _peers:
                self._offer_same_tier_enrichment(result, _peers)
            self._retain_hint_coded_dlsite_urls()

        missing = self._get_missing_fields()
        if missing:
            self._status_lbl.setText(
                t('add_game.fields_still_missing', fields=", ".join(missing)))
            fs = scaled(12, self)
            self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:{fs}px;")
        return True

    # ── Shared result diff/apply helpers ──────────────────────────────────
    # Used by _process_search_result (acceptance), the same-tier merge
    # preview (_offer_same_tier_enrichment/_build_merge_model) AND
    # CandidatePreviewDialog (live per-candidate preview while browsing) —
    # single source of truth so the popup never shows something different
    # from what confirming actually applies.

    def _extract_result_year(self, result) -> str:
        """4-digit year out of a GameInfo.release_date string, if any."""
        if getattr(result, 'release_date', ''):
            import re as _re_yr
            m = _re_yr.search(r'\b(19|20)\d{2}\b', result.release_date)
            if m:
                return m.group(0)
        return ''

    def _compute_candidate_diff(self, result) -> dict:
        """Compute what accepting *result* (a GameInfo) would change vs.
        the data currently in the form — the single source of truth for
        both the candidate-preview popup (live, per-candidate, purely for
        display) and the case (init / overwrite / enrich) selection used
        when actually applying a confirmed candidate.

        Returns a dict with:
          has_existing        — form already has saved data
          is_overwrite         — always False for search apply now (kept for
                                  preview callers); filled fields are never
                                  replaced just because text differs
          promote_primary      — current primary page is unreachable; confirming
                                  may adopt this source as the new primary
                                  marker without wiping saved fields
          same_origin          — candidate source matches the saved primary
          same_origin_no_diff  — source already applied and no material news
                                  (carousel filters these out)
          has_enrich           — something additive to fill/union
          has_changes           — worth showing / applying (material news or
                                  soft primary promote)
          result_year           — extracted 4-digit year, if any
          fields                — {field: {'old': str|None, 'new': str}} —
                                   only EMPTY→fill (or rename); never a
                                   description replacement driven by rewrite
          new_tags / new_urls   — additive (tags/URLs are always a union,
                                  never cleared)
          new_image             — whether a cover would be set (only if none)
          new_reviews           — the source's own verdict, when it isn't
                                  already on the form (one per source)
        """
        current_name = self._name_edit.text().strip()
        current_desc = self._desc_edit.toPlainText().strip()
        current_dev  = self._developer_edit.text().strip()
        current_year = self._year_edit.text().strip()
        current_tags = set(getattr(self, '_tags', []) or [])
        has_image    = bool(self._original_image_path or getattr(self, '_image_path', ''))
        # NOTE: also checks developer/year, not just desc/image/tags — a
        # game with ONLY those two manually filled in (e.g. VNDB had no
        # description, the user typed just the developer name) still HAS
        # existing data. Missing this let a same-source result offering
        # nothing new get treated as a blank slate instead of silently
        # skipped (same_origin_no_diff below never got a chance to fire).
        has_existing = bool(current_desc or has_image or current_tags or current_dev or current_year)

        raw_source  = getattr(result, 'source', '') or ''
        result_year = self._extract_result_year(result)

        _fp = getattr(self, '_enrichment_source_fingerprint', {}) or {}
        _existing_src  = _fp.get('source', '') or ''
        _existing_src_base = (_existing_src or '').split('+')[0]
        _result_src_base   = (raw_source or '').split('+')[0]
        _is_same_origin = bool(
            _existing_src_base and _result_src_base and
            _existing_src_base == _result_src_base
        )

        # Canonical (case/separator-insensitive) comparison, matching
        # _apply_web_tags's own dedup key — a raw exact-string check here
        # kept flagging an already-saved tag as "new" whenever a fresh fetch
        # returned it with different casing/separators (e.g. saved as
        # "female protagonist", VNDB now returns "Female Protagonist").
        from core.library import tag_merge_key
        _current_tag_keys = {tag_merge_key(x) for x in current_tags}
        new_tags = [g for g in (result.genres or [])
                    if g and tag_merge_key(g) not in _current_tag_keys]
        new_urls = self._new_result_site_urls(result)
        new_image = bool(result.image_url and not has_image)
        new_reviews = self._new_result_reviews(result)

        fills_empty = bool(
            (result.description and not current_desc)
            or new_image
            or (getattr(result, 'developer', '') and not current_dev)
            or (result_year and not current_year)
        )
        name_change = bool(result.name and result.name != current_name)
        # Material news: empty-field fills, additive tags/urls/reviews, or a
        # confirmed rename. A different description while one is already
        # saved is NOT material — that used to force primary overwrite.
        has_material = bool(
            fills_empty or name_change or new_tags or new_urls or new_reviews
        )

        already_applied = bool(
            _result_src_base and _result_src_base in self._applied_enrichment_sources()
        )
        # Soft-promote only when the saved primary page is gone AND this
        # candidate is a different source. Never promote on text rewrite.
        promote_primary = bool(
            has_existing and _existing_src_base and not _is_same_origin
            and not self._is_primary_source_reachable()
        )

        entry = getattr(self, '_entry', None)
        is_from_bulk_auto = bool(
            entry and (
                getattr(entry, 'auto_added', False)
                or getattr(entry, 'requires_confirmation', False)
                or (getattr(entry, 'cloud_metadata', {}) or {}).get('auto_enriched', False)
            )
        )

        is_overwrite = bool(has_existing and is_from_bulk_auto)
        same_origin_no_diff = bool(already_applied and not has_material
                                   and not promote_primary)

        # ── Per-field diff (for display — showing proposed changes) ─────
        fields: dict = {}
        if is_from_bulk_auto or not has_existing:
            if name_change:
                fields['name'] = {'old': current_name or None, 'new': result.name}
            _rd = result.description or ''
            if _rd and _rd != current_desc:
                fields['description'] = {'old': current_desc or None, 'new': _rd}
            _rv = getattr(result, 'developer', '') or ''
            if _rv and _rv != current_dev:
                fields['developer'] = {'old': current_dev or None, 'new': _rv}
            if result_year and result_year != current_year:
                fields['year'] = {'old': current_year or None, 'new': result_year}
        else:
            # User-managed game: description only shows as a fill (never a
            # rewrite of typed text) — but name/year/developer show whenever
            # they DIFFER, filled or not, purely for display; nothing about
            # how they're actually applied changes: name is still the only
            # one of the three unconditionally written on confirm (see
            # _apply_result_init/_apply_result_overwrite), year/developer
            # still only fill when empty. The diff is what was invisible
            # before ("nowhere in the preview was shown the year [[or
            # developer/name]]" — this dict feeds the candidate-preview meta
            # line AND the merge dialog's field options), not what gets
            # applied.
            if name_change:
                fields['name'] = {'old': current_name or None, 'new': result.name}
            if result_year and result_year != current_year:
                fields['year'] = {'old': current_year or None, 'new': result_year}
            _rv = getattr(result, 'developer', '') or ''
            if _rv and _rv != current_dev:
                fields['developer'] = {'old': current_dev or None, 'new': _rv}
            if not current_desc and result.description:
                fields['description'] = {'old': None, 'new': result.description}

        has_enrich = bool(has_material or (has_existing and bool(fields)))
        has_changes = bool(has_material or promote_primary or (has_existing and bool(fields)))

        return {
            'has_existing': has_existing,
            'is_overwrite': is_overwrite,
            'promote_primary': promote_primary,
            'same_origin': _is_same_origin,
            'same_origin_no_diff': same_origin_no_diff,
            'already_applied': already_applied,
            'name_change': name_change,
            'has_material': has_material,
            'has_enrich': has_enrich,
            'has_changes': has_changes,
            'result_year': result_year,
            'fields': fields,
            'new_tags': new_tags,
            'new_urls': new_urls,
            'new_image': new_image,
            'new_reviews': new_reviews,
            # What's on the form right now, for the preview to tell "this
            # candidate's value already matches what I have" (✓, like an
            # already-saved tag) apart from "this fills a field that was
            # empty" (+, like a genuinely new tag) — fields[field] alone
            # only distinguishes a genuine DIFFERENCE from everything else.
            'current': {
                'name': current_name, 'description': current_desc,
                'developer': current_dev, 'year': current_year,
            },
        }

    def _current_image_url(self) -> str | None:
        """The source URL the currently-set image came from, if known.

        Both apply paths used to re-download unconditionally whenever a
        candidate had ANY image_url — even the exact same one already
        applied, every single re-search. _image_url_cache/_image_path_to_url
        already tracked url<->local-path but nothing ever consulted them.
        """
        path = getattr(self, '_image_path', None)
        if not path:
            return None
        return (getattr(self, '_image_path_to_url', None) or {}).get(path)

    def _apply_result_init(self, result):
        """Case B — no existing data: fill all empty fields (union for
        tags). Name/genres are unconditional; description/developer/year/
        image only fill if currently empty (a field the user typed — or an
        image the user already has — by hand even in an otherwise-blank
        form is never overwritten here).

        Image used to be the one unconditional exception (downloaded and
        set as the new current cover whenever the URL merely DIFFERED from
        today's), which silently swapped an already-set cover on every
        confirm — exactly the "overwritten, not added" behaviour images are
        supposed never to have (they're additive, like tags: a differing
        candidate cover belongs in the merge dialog's checked-by-default
        chip, not applied here without asking)."""
        current_name = self._name_edit.text().strip()
        current_desc = self._desc_edit.toPlainText().strip()
        current_dev  = self._developer_edit.text().strip()
        current_year = self._year_edit.text().strip()
        has_image = bool(self._original_image_path or getattr(self, '_image_path', ''))
        if result.name and result.name != current_name:
            self._name_edit.setText(result.name)
        if result.image_url and not has_image:
            self._download_and_set_image(result.image_url)
        if result.description and not current_desc:
            self._desc_edit.setPlainText(result.description)
        if result.genres:
            self._apply_web_tags(result.genres)
        if getattr(result, 'developer', '') and not current_dev:
            self._developer_edit.setText(result.developer)
        _ry = self._extract_result_year(result)
        if _ry and not current_year:
            self._year_edit.setText(_ry)
        self._merge_result_urls(result)
        self._merge_result_review(result)
        if hasattr(self, '_rebuild_tag_chips'):
            self._rebuild_tag_chips()

    def _apply_result_overwrite(self, result):
        """Case C/D — better tier or significantly different: replace
        fields that exist in the new source, keep existing values for
        absent fields. Tags are always a union (never cleared)."""
        if result.name:
            self._name_edit.setText(result.name)
        if result.image_url and result.image_url != self._current_image_url():
            self._download_and_set_image(result.image_url)
        if result.description:
            self._desc_edit.setPlainText(result.description)
        if result.genres:
            self._apply_web_tags(result.genres)
        if getattr(result, 'developer', ''):
            self._developer_edit.setText(result.developer)
        _ry = self._extract_result_year(result)
        if _ry:
            self._year_edit.setText(_ry)
        self._merge_result_urls(result)
        self._merge_result_review(result)
        if hasattr(self, '_rebuild_tag_chips'):
            self._rebuild_tag_chips()

    def _merge_result_review(self, result):
        """Keep the source's verdict(s) as reviews of their own.

        Every tier does this — a rating is not something one tier owns and
        another ignores — and the source travels with each review, so where a
        score came from is answerable long after the search. A site that
        ships many user reviews (DLsite) contributes the whole list.
        """
        if hasattr(result, "as_reviews"):
            reviews = result.as_reviews()
        elif hasattr(result, "as_review"):
            one = result.as_review()
            reviews = [one] if one else []
        else:
            reviews = []
        if reviews:
            self._merge_reviews(reviews)

    def _merge_reviews(self, reviews: list):
        """Fold *reviews* into the form, keyed by review_identity.

        A single-verdict site (Steam/VNDB) still occupies one slot keyed by
        source; a multi-review site keeps every user review distinct. The
        user's own reviews (source "user") are never overwritten by a web
        import — different identity — and are never touched here either.
        """
        from core.library import review_identity
        merged = list(getattr(self, "_reviews", None) or [])
        by_key = {review_identity(r): i
                  for i, r in enumerate(merged) if isinstance(r, dict)}
        for review in reviews:
            if not isinstance(review, dict):
                continue
            if (review.get("source") or "") == "user":
                continue
            key = review_identity(review)
            if not key:
                continue
            idx = by_key.get(key)
            if idx is not None:
                merged[idx] = review
            else:
                by_key[key] = len(merged)
                merged.append(review)
        self._reviews = merged
        if hasattr(self, "_update_reviews_btn"):
            self._update_reviews_btn()

    def _new_result_reviews(self, result) -> list:
        """Reviews the source would add that the form does not already have.

        Identity is per review (see review_identity), so a DLsite page with
        ten user reviews can contribute the ones that are new without the
        whole set being dropped because one of them was already imported.
        """
        from core.library import review_identity
        if hasattr(result, "as_reviews"):
            incoming = result.as_reviews()
        elif hasattr(result, "as_review"):
            one = result.as_review()
            incoming = [one] if one else []
        else:
            incoming = []
        if not incoming:
            return []
        have = {review_identity(r): r
                for r in (getattr(self, "_reviews", None) or [])
                if isinstance(r, dict)}
        fresh = []
        for review in incoming:
            key = review_identity(review)
            if not key:
                continue
            existing = have.get(key)
            if existing and all(
                    str(existing.get(k, "")) == str(review.get(k, ""))
                    for k in ("rating", "reviewer", "text")):
                continue
            fresh.append(review)
        return fresh

    def _apply_result_enrich(self, result, new_tags: list):
        """Case E — same/lower tier: only fill EMPTY fields; tags always
        union (additive, never replaces or clears existing tags).

        The title is the exception: the candidate preview already showed the
        rename with a strikethrough, and accepting that candidate means the
        rename — leaving the old name in place after the user confirmed the
        new one is what made "already saved" games keep the wrong title.
        """
        current_name = self._name_edit.text().strip()
        current_desc = self._desc_edit.toPlainText().strip()
        current_dev  = self._developer_edit.text().strip()
        current_year = self._year_edit.text().strip()
        if result.name and result.name != current_name:
            self._name_edit.setText(result.name)
        if result.description and not current_desc:
            self._desc_edit.setPlainText(result.description)
        if result.image_url and not (
                self._original_image_path or getattr(self, '_image_path', '')
        ):
            self._download_and_set_image(result.image_url)
        if getattr(result, 'developer', '') and not current_dev:
            self._developer_edit.setText(result.developer)
        _ry = self._extract_result_year(result)
        if _ry and not current_year:
            self._year_edit.setText(_ry)
        if new_tags:
            self._apply_web_tags(new_tags)
        self._merge_result_urls(result)
        self._merge_result_review(result)
        if hasattr(self, '_rebuild_tag_chips'):
            self._rebuild_tag_chips()

    def _store_result_fingerprint(self, src: str, result, result_year: str,
                                  *, as_primary: bool = True):
        """Remember primary source + every source that contributed.

        *as_primary* False keeps the existing primary marker (enrich from
        another site) so a later Steam hit is still recognized as already
        applied after a VNDB fill-in. Soft-promote / first apply / same
        origin pass True.
        """
        prev = getattr(self, '_enrichment_source_fingerprint', None) or {}
        applied = list(prev.get('applied') or [])
        base = (src or '').split('+')[0]
        if base and base not in applied:
            applied.append(base)
        for s in self._applied_enrichment_sources():
            if s not in applied:
                applied.append(s)
        primary = src if as_primary or not prev.get('source') else prev.get('source')
        self._enrichment_source_fingerprint = {
            'source': primary or src,
            'name':   result.name or prev.get('name', ''),
            'content': (
                (result.description or '') + ' ' +
                (getattr(result, 'developer', '') or '') + ' ' +
                (result_year or '')
            ).strip() if as_primary or not prev.get('content') else prev.get('content', ''),
            'applied': applied,
        }
        if as_primary:
            self._invalidate_primary_reachability()

    def _mark_source_applied(self, src: str) -> None:
        """Record that *src* contributed (merge chips, URL fetch, …)."""
        base = (src or '').split('+')[0]
        if not base or base in ('user', 'web'):
            return
        fp = getattr(self, '_enrichment_source_fingerprint', None)
        if not isinstance(fp, dict):
            self._enrichment_source_fingerprint = {
                'source': '', 'content': '', 'applied': [base],
            }
            return
        applied = list(fp.get('applied') or [])
        if base not in applied:
            applied.append(base)
            fp['applied'] = applied

    @staticmethod
    def _source_from_url(url: str) -> str:
        """Map a store/page URL to a known source id, or ''."""
        u = (url or '').lower()
        if not u:
            return ''
        hints = (
            ('store.steampowered.com', 'steam'),
            ('steampowered.com', 'steam'),
            ('vndb.org', 'vndb'),
            ('itch.io', 'itch'),
            ('dlsite.com', 'dlsite'),
            ('mobygames.com', 'mobygames'),
            ('wikipedia.org', 'wikipedia'),
            ('pcgamingwiki.com', 'pcgamingwiki'),
        )
        for needle, src in hints:
            if needle in u:
                return src
        return ''

    def _applied_enrichment_sources(self) -> set[str]:
        """Sources already reflected on the form (fingerprint, reviews, URLs)."""
        out: set[str] = set()
        fp = getattr(self, '_enrichment_source_fingerprint', None) or {}
        for s in fp.get('applied') or []:
            b = (s or '').split('+')[0]
            if b:
                out.add(b)
        primary = (fp.get('source') or '').split('+')[0]
        if primary:
            out.add(primary)
        for r in (getattr(self, '_reviews', None) or []):
            if not isinstance(r, dict):
                continue
            b = (r.get('source') or '').split('+')[0]
            if b and b not in ('user', 'web'):
                out.add(b)
        for u in (getattr(self, '_store_urls', None) or []):
            b = self._source_from_url(u)
            if b:
                out.add(b)
        return out

    def _invalidate_primary_reachability(self) -> None:
        self._primary_reachability_cache = None
        self._primary_reachability_gen = getattr(self, '_primary_reachability_gen', 0) + 1

    def _primary_urls_to_probe(self) -> list[str]:
        """Store URLs that belong to the saved primary source, if any."""
        fp = getattr(self, '_enrichment_source_fingerprint', None) or {}
        primary = (fp.get('source') or '').split('+')[0]
        if not primary:
            return []
        return [u for u in (getattr(self, '_store_urls', None) or [])
                if self._source_from_url(u) == primary]

    def _is_primary_source_reachable(self) -> bool:
        """Whether the saved primary's page still answers.

        Reads only the cache filled by `_start_primary_reachability_probe`.
        Unknown (probe pending / not started) → True (optimistic): never
        soft-promote until a background probe has proven the page dead, and
        never block the GUI thread on open_url.
        """
        cached = getattr(self, '_primary_reachability_cache', None)
        if cached is None:
            return True
        return bool(cached)

    def _start_primary_reachability_probe(self) -> bool:
        """Probe primary URLs on a worker thread. Returns True if a probe
        was started (caller may defer UI that depends on promote_primary)."""
        urls = self._primary_urls_to_probe()
        if not urls:
            self._primary_reachability_cache = True
            return False
        gen = getattr(self, '_primary_reachability_gen', 0)
        probe_urls = list(urls[:2])

        def _bg(urls=probe_urls, gen=gen):
            ok = False
            try:
                for u in urls:
                    if self._probe_page_reachable(u):
                        ok = True
                        break
            except Exception as e:
                logger.debug(f"Primary reachability probe crashed: {e}")
                ok = False
            try:
                self.primary_reachability_done.emit(gen, ok)
            except RuntimeError:
                pass  # dialog already destroyed

        threading.Thread(target=_bg, daemon=True).start()
        return True

    def _on_primary_reachability_done(self, gen: int, reachable: bool):
        """GUI-thread slot: apply probe result and refresh candidate UI."""
        if gen != getattr(self, '_primary_reachability_gen', 0):
            return
        if not self.isVisible():
            return
        self._primary_reachability_cache = bool(reachable)
        results = getattr(self, '_pending_reachability_results', None)
        if not results:
            return
        useful = self._dedupe_except_description([
            r for r in results
            if self._compute_candidate_diff(r).get('has_changes')
        ])

        dlg = getattr(self, '_active_candidate_dialog', None)
        if dlg is not None:
            if useful:
                try:
                    dlg.set_candidates(useful)
                except RuntimeError:
                    pass
            return

        if not getattr(self, '_candidate_show_deferred', False):
            return
        self._candidate_show_deferred = False
        if useful:
            self._open_candidate_carousel(useful)
        else:
            self._status_lbl.setText(t('add_game.candidate_no_changes'))
            self._status_lbl.setStyleSheet(
                f"color:{palette('text_secondary')};font-size:{scaled(12, self)}px;")

    @staticmethod
    def _probe_page_reachable(url: str, timeout: float = 4.0) -> bool:
        """Cheap GET: HTTP < 400 counts as alive. Network errors → dead.

        Must only run off the GUI thread (see `_start_primary_reachability_probe`).
        """
        if not url:
            return False
        try:
            import urllib.request
            from core.net import open_url
            req = urllib.request.Request(
                url,
                headers={'User-Agent': 'SaveSync/1.0 (enrichment reachability)'},
                method='GET',
            )
            with open_url(req, timeout=timeout) as resp:
                code = getattr(resp, 'status', None) or resp.getcode()
                return 200 <= int(code) < 400
        except Exception as e:
            logger.debug(f"Primary source probe failed for {url!r}: {e}")
            return False

    # ── Enrichment chain ─────────────────────────────────────────────────────

    # ── Enrichment tier authority map ─────────────────────────────────────────

    def _get_source_tier(self, source: str) -> str:
        """Return the authority tier ('api' | 'trusted' | 'generic') for a source."""
        _API     = {'steam', 'pcgamingwiki', 'vndb'}
        _TRUSTED = {'itch', 'dlsite', 'mobygames', 'wikipedia'}
        base = source.split('+')[0] if '+' in source else source
        if base in _API:     return 'api'
        if base in _TRUSTED: return 'trusted'
        return 'generic'

    def _infer_source_from_path(self, path_or_url: str) -> str:
        """Heuristically infer the game's platform from an exe path or launcher URL.

        Returns a recognised info_source key ('steam', 'itch', …) or '' when
        nothing can be detected.  Used by drag-and-drop / file-browse so that
        info_source is seeded even before an explicit API search is run, making
        the tier-comparison logic behave consistently with the API-search path.
        Only the sources present in _get_source_tier are returned so that tier
        decisions are always meaningful.
        """
        s = (path_or_url or '').replace('\\', '/').lower()
        if 'steam://' in s or '/steamapps/common/' in s or '/steam/steamapps/' in s:
            return 'steam'
        if 'itch://' in s or '/itch/apps/' in s or 'itch.io' in s:
            return 'itch'
        return ''

    def _seed_fingerprint_from_path(self, path_or_url: str) -> None:
        """Seed _enrichment_source_fingerprint from a path/URL if not already set by a search."""
        _src = self._infer_source_from_path(path_or_url)
        if _src and not (getattr(self, '_enrichment_source_fingerprint', {}) or {}).get('source'):
            self._enrichment_source_fingerprint = {
                'source': _src, 'content': '', 'applied': [_src],
            }

    def _source_content_similarity(self, text1: str, text2: str) -> float:
        """Jaccard word-overlap similarity between two strings (0.0 – 1.0)."""
        w1 = set((text1 or '').lower().split())
        w2 = set((text2 or '').lower().split())
        if not w1 and not w2: return 1.0
        if not w1 or not w2:  return 0.0
        return len(w1 & w2) / len(w1 | w2)

    def _payload_fingerprint_except_desc(self, result) -> tuple:
        """Identity of a candidate ignoring description text.

        Two sources that match on this fingerprint and only differ in
        description are treated as duplicates — the later one is not
        proposed at all. Extra tags, URLs, ratings, … change the
        fingerprint, so that fuller source is proposed complete (desc
        included).
        """
        name = (getattr(result, 'name', '') or '').strip().casefold()
        dev = (getattr(result, 'developer', '') or '').strip().casefold()
        year = self._extract_result_year(result)
        img = (getattr(result, 'image_url', '') or '').strip()
        if "/thumb/" in img:
            img = img.replace("/thumb/", "/", 1)
        tags = tuple(sorted({
            g.strip().casefold()
            for g in (getattr(result, 'genres', None) or [])
            if (g or '').strip()
        }))
        urls = []
        for u in (
            [getattr(result, 'store_url', '') or '']
            + list(getattr(result, 'extra_urls', None) or [])
        ):
            u = (u or '').strip()
            if u and u not in urls:
                urls.append(u)
        try:
            rating = round(float(getattr(result, 'rating', 0) or 0), 2)
        except (TypeError, ValueError):
            rating = 0.0
        try:
            votes = int(getattr(result, 'vote_count', 0) or 0)
        except (TypeError, ValueError):
            votes = 0
        rev_n = len(getattr(result, 'reviews', None) or [])
        return (name, dev, year, img, tags, tuple(urls), rating, votes, rev_n)

    def _dedupe_except_description(self, results: list) -> list:
        """Drop a source that is 1:1 with an earlier one except description.

        Kept sources are always proposed whole (whatever fields they have,
        description included). No merging/picking of the "better" prose —
        a prose-only twin is simply not offered.
        """
        kept: list = []
        seen: set = set()
        for r in results:
            fp = self._payload_fingerprint_except_desc(r)
            if fp in seen:
                continue
            seen.add(fp)
            kept.append(r)
        return kept

    def _same_tier_peers(self, base_result, others: list) -> list:
        """Peers for chip enrichment after a candidate is confirmed.

        Same *tier* only (no lower-tier search). The confirmed *source* is
        excluded entirely — picking Steam title 1 must not re-offer Steam
        title 2. Other sources keep every distinct title (VNDB 1 + VNDB 2).
        A peer that matches the confirmed candidate on everything except
        description is skipped on purpose (no chip merge for prose-only twin).
        """
        _base_src = (getattr(base_result, 'source', '') or '').split('+')[0]
        _base_tier = self._get_source_tier(_base_src)
        _base_fp = self._payload_fingerprint_except_desc(base_result)
        peers = []
        for r in others:
            if r is base_result:
                continue
            _r_name = getattr(r, 'name', '') or ''
            src = (getattr(r, 'source', '') or '').split('+')[0]
            if src and _base_src and src == _base_src:
                logger.info(
                    f"_same_tier_peers: SKIP {src!r}/{_r_name!r} — "
                    f"same source as confirmed {_base_src!r}")
                continue               # same source already declared
            _r_tier = self._get_source_tier(src)
            if _r_tier != _base_tier:
                logger.info(
                    f"_same_tier_peers: SKIP {src!r}/{_r_name!r} — "
                    f"tier {_r_tier!r} != confirmed tier {_base_tier!r} ({_base_src!r})")
                continue
            if self._payload_fingerprint_except_desc(r) == _base_fp:
                logger.info(
                    f"_same_tier_peers: SKIP {src!r}/{_r_name!r} — "
                    f"identical to confirmed candidate except description")
                continue               # 1:1 except description → skip
            logger.info(f"_same_tier_peers: KEEP {src!r}/{_r_name!r} as peer")
            peers.append(r)
        return peers

    @staticmethod
    def _peer_section_key(info, index: int) -> str:
        """Composite key: ``source · title · url`` so same-source peers stay distinct."""
        src = (getattr(info, 'source', '') or 'web').split('+')[0] or 'web'
        name = (getattr(info, 'name', '') or '').strip() or f'#{index}'
        url = _inspect_url(info) or (getattr(info, 'image_url', '') or '').strip()
        return f"{src} · {name} · {url or index}"

    @staticmethod
    def _peer_section_label(src_label: str, title: str, url: str) -> str:
        """Human header matching the composite key, URL shortened for space."""
        bits = [src_label]
        if title:
            bits.append(title)
        if url:
            # Host + short path — enough to tell two VNDB/Steam pages apart.
            try:
                from urllib.parse import urlparse
                p = urlparse(url)
                host = (p.netloc or "").removeprefix("www.")
                path = (p.path or "").rstrip("/")
                tail = path.rsplit("/", 1)[-1] if path else ""
                short = f"{host}/{tail}" if host and tail else (host or url)
            except Exception:
                short = url
            if len(short) > 42:
                short = short[:41] + "…"
            bits.append(short)
        return " · ".join(bits)

    def _run_same_tier_merge(self, base_result, pre_confirm_name: str = '') -> bool:
        """Offer peer enrichment chips. Returns True when the user asked to
        go back to the candidate carousel (form snapshot must be restored).

        *pre_confirm_name* is what the name field held right before this
        candidate was confirmed (the carousel's form snapshot) — name is
        applied unconditionally on confirm (never fill-only), so by the time
        this runs the ORIGINAL name is already gone from the form; this is
        the only way to still offer "go back to what it was" as a choice."""
        _pool = getattr(self, '_last_search_candidates', None) or []
        logger.info(
            f"_run_same_tier_merge: base={getattr(base_result, 'source', '')!r}/"
            f"{getattr(base_result, 'name', '')!r} pool_size={len(_pool)}")
        peers = self._same_tier_peers(base_result, _pool)
        if not peers:
            logger.info(
                "_run_same_tier_merge: no same-tier peers — still checking "
                "the confirmed candidate's own year/name against what's saved")
        # base_result is passed even with zero peers: it may still offer its
        # OWN year as an option (see _build_merge_model) when it came from a
        # different tier than whatever is currently saved.
        model = self._build_merge_model(
            peers, base_result=base_result, pre_confirm_name=pre_confirm_name)
        if not model.get('has_options'):
            logger.info(
                "_run_same_tier_merge: peers found "
                f"({[getattr(p, 'source', '') for p in peers]!r}) but "
                f"_build_merge_model produced no options (current="
                f"{model.get('current')!r}) — no dialog")
            return False
        logger.info(
            f"_run_same_tier_merge: showing merge dialog — "
            f"name={len(model.get('name') or [])} "
            f"description={len(model.get('description') or [])} "
            f"developer={len(model.get('developer') or [])} "
            f"year={len(model.get('year') or [])} "
            f"images={len(model.get('images') or [])} "
            f"tags={len(model.get('tags') or [])} "
            f"urls={len(model.get('urls') or [])} "
            f"reviews={len(model.get('reviews') or [])}")
        dlg = EnrichmentMergeDialog(model, self._source_label, self)
        code = dlg.exec()
        if code == EnrichmentMergeDialog.RESULT_BACK:
            return True
        if code == QDialog.DialogCode.Accepted:
            self._apply_merge_selection(dlg.selection())
            missing = self._get_missing_fields()
            if missing:
                self._status_lbl.setText(
                    t('add_game.fields_still_missing', fields=", ".join(missing)))
                self._status_lbl.setStyleSheet(
                    f"color:{palette('warning')};font-size:{scaled(12, self)}px;")
        return False

    def _offer_same_tier_enrichment(self, base_result, others: list):
        """Legacy entry: merge without a Back path (no form snapshot)."""
        peers = self._same_tier_peers(base_result, others)
        if not peers:
            return
        model = self._build_merge_model(peers)
        if not model.get('has_options'):
            return
        dlg = EnrichmentMergeDialog(model, self._source_label, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self._apply_merge_selection(dlg.selection())

    def _capture_search_form(self) -> dict:
        """Snapshot fields a search apply may change, for merge-dialog Back."""
        return {
            'name': self._name_edit.text(),
            'desc': self._desc_edit.toPlainText(),
            'dev': self._developer_edit.text(),
            'year': self._year_edit.text(),
            'tags': list(getattr(self, '_tags', []) or []),
            'urls': list(getattr(self, '_store_urls', []) or []),
            'reviews': [dict(r) for r in (getattr(self, '_reviews', None) or [])],
            'image_path': getattr(self, '_image_path', None),
            'original_image_path': getattr(self, '_original_image_path', None),
            'detected_images': list(getattr(self, '_detected_images', []) or []),
            'current_image_idx': getattr(self, '_current_image_idx', 0),
            'fingerprint': dict(
                getattr(self, '_enrichment_source_fingerprint', None) or {}),
        }

    def _restore_search_form(self, snap: dict):
        """Undo a provisional candidate apply so the carousel can reopen."""
        self._name_edit.setText(snap.get('name', ''))
        self._desc_edit.setPlainText(snap.get('desc', ''))
        self._developer_edit.setText(snap.get('dev', ''))
        self._year_edit.setText(snap.get('year', ''))
        self._tags = list(snap.get('tags') or [])
        self._store_urls = list(snap.get('urls') or [])
        self._reviews = [dict(r) for r in (snap.get('reviews') or [])]
        self._image_path = snap.get('image_path')
        self._original_image_path = snap.get('original_image_path')
        self._detected_images = list(snap.get('detected_images') or [])
        self._current_image_idx = snap.get('current_image_idx') or 0
        self._enrichment_source_fingerprint = dict(snap.get('fingerprint') or {})
        if hasattr(self, '_rebuild_tag_chips'):
            self._rebuild_tag_chips()
        if hasattr(self, '_rebuild_url_chips'):
            self._rebuild_url_chips()
        if hasattr(self, '_update_reviews_btn'):
            self._update_reviews_btn()
        if hasattr(self, '_update_image_preview'):
            self._update_image_preview(self._image_path or '')
        self._status_lbl.setText(
            t('add_game.candidates_found',
              n=len(getattr(self, '_last_search_candidates', []) or []))
            if len(getattr(self, '_last_search_candidates', []) or []) > 1
            else t('add_game.candidate_found_single')
        )
        fs = scaled(12, self)
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:{fs}px;")

    def _build_merge_model(self, collected: list, *, base_result=None,
                           pre_confirm_name: str = '') -> dict:
        """Per-field option lists for the merge preview.

        description is fill-only: the CONFIRMED candidate is authoritative,
        so it is never offered for replacement — peers only compete for it
        while still EMPTY. Name, developer, year and image are the
        exceptions — see their own comments below for why. Each peer title
        is its own section (``vndb::Title::0``), so two VNDB hits stay
        distinguishable. Tags/URLs expand additively; reviews are one chip
        per peer (same API source identity still collapses on apply).

        *base_result*, when given, is the just-confirmed candidate itself —
        used only to offer ITS OWN year/developer as options (see below); it
        is never added as a tag/url/review source (those already came from
        it via the normal apply, offering them again would just duplicate).

        *pre_confirm_name*, when given, is what the name field held right
        before *base_result* was confirmed — see below.
        """
        cur_name = self._name_edit.text().strip()
        cur_desc = self._desc_edit.toPlainText().strip()
        cur_dev  = self._developer_edit.text().strip()
        cur_year = self._year_edit.text().strip()
        from core.library import tag_merge_key
        cur_tags = {tag_merge_key(x) for x in (getattr(self, '_tags', []) or [])}
        has_img  = bool(self._original_image_path or getattr(self, '_image_path', ''))
        cur_image_url = self._current_image_url() or ''

        peer_keys: list[str] = []
        for i, info in enumerate(collected):
            peer_keys.append(self._peer_section_key(info, i))

        def _opts(getter, exclude: str = ''):
            opts, seen = [], set()
            _excl = (exclude or '').strip().lower()
            for info, pkey in zip(collected, peer_keys):
                v = (getter(info) or '').strip()
                if not v or v.lower() in seen or v.lower() == _excl:
                    continue
                seen.add(v.lower())
                opts.append({'source': pkey, 'value': v})
            return opts

        model = {
            'current': {
                'name': cur_name, 'description': cur_desc, 'developer': cur_dev,
                'year': cur_year, 'has_image': has_img,
            },
            # Name/description/year are offered even though a value is
            # already set — only a DIFFERING value shows, and it's never
            # auto-selected (EnrichmentMergeDialog adds an explicit,
            # pre-checked "Keep current" chip alongside it) — see that
            # file's _build(). developer stays fill-only and DOES
            # auto-select its first offer — extending it the same way would
            # need the same "keep current" treatment first, not just
            # dropping the `[] if cur_dev else` guard — now given the same
            # "keep current" treatment (see EnrichmentMergeDialog._build,
            # which is already field-agnostic here). Images are NEITHER
            # of those — a game can hold many covers (the carousel in the
            # add/edit dialog), so a peer's differing image is additive,
            # exactly like tags/urls below: checked by default, never
            # replacing the current cover, just offered alongside it.
            'name':        _opts(lambda i: getattr(i, 'name', ''), exclude=cur_name),
            'description': _opts(lambda i: i.description, exclude=cur_desc),
            'developer':   _opts(lambda i: getattr(i, 'developer', ''), exclude=cur_dev),
            'year':        _opts(lambda i: self._extract_result_year(i), exclude=cur_year),
            'images': [],
            'tags': [],
            'urls': [],
            'reviews': [],
            'source_meta': {},
        }
        seen_tags = set(cur_tags)
        seen_urls: set[str] = set()
        seen_images = {cur_image_url} if cur_image_url else set()
        # Review slot is per API source id (steam/vndb…): two VNDB titles
        # share one stored identity, so only the first peer offers reviews.
        seen_review_api: set[str] = set()
        for info, pkey in zip(collected, peer_keys):
            src_id = (info.source or 'web').split('+')[0] or 'web'
            title = (getattr(info, 'name', '') or '').strip()
            inspect = _inspect_url(info)
            cover = (getattr(info, 'image_url', '') or '').strip()
            model['source_meta'][pkey] = {
                'inspect_url': inspect,
                'image_url': cover,
                'name': title,
                'source_id': src_id,
                'label': self._peer_section_label(
                    self._source_label(src_id), title, inspect),
            }
            if cover and cover not in seen_images:
                seen_images.add(cover)
                model['images'].append({'source': pkey, 'value': cover})
            for g in (info.genres or []):
                _gk = tag_merge_key(g)
                if _gk in seen_tags:
                    continue
                seen_tags.add(_gk)
                model['tags'].append({'source': pkey, 'value': g})
            for u in self._new_result_site_urls(info):
                if u in seen_urls:
                    continue
                seen_urls.add(u)
                model['urls'].append({'source': pkey, 'value': u})
            if src_id in seen_review_api:
                continue
            _revs = self._new_result_reviews(info)
            if _revs:
                seen_review_api.add(src_id)
                model['reviews'].append({'source': pkey, 'value': _revs})

        # The just-confirmed candidate's OWN year/developer, offered even
        # with ZERO same-tier peers. Both are fill-only on apply (never
        # replace a saved value), so a candidate found in a LATER tier than
        # what's already saved — e.g. a forum result confirmed after Steam —
        # would otherwise have its differing year/developer silently
        # discarded with nothing around to ever surface the disagreement:
        # same-tier peers can only ever come from the SAME search batch as
        # the confirmed candidate, never from an earlier, separate tier's
        # search (and _opts above only looks at `collected`, i.e. peers).
        if base_result is not None:
            _bkey = self._peer_section_key(base_result, -1)

            def _ensure_base_source_meta():
                if _bkey in model['source_meta']:
                    return
                _bsrc = (getattr(base_result, 'source', '') or 'web').split('+')[0] or 'web'
                _btitle = (getattr(base_result, 'name', '') or '').strip()
                _binspect = _inspect_url(base_result)
                model['source_meta'][_bkey] = {
                    'inspect_url': _binspect,
                    'image_url': (getattr(base_result, 'image_url', '') or '').strip(),
                    'name': _btitle,
                    'source_id': _bsrc,
                    'label': self._peer_section_label(
                        self._source_label(_bsrc), _btitle, _binspect),
                }

            def _base_offer(field: str, value: str):
                _v = (value or '').strip()
                _cur = (model['current'].get(field) or '').strip()
                if not _v or _v.lower() == _cur.lower():
                    return
                if any(o['value'].lower() == _v.lower() for o in model[field]):
                    return
                _ensure_base_source_meta()
                model[field].append({'source': _bkey, 'value': _v})

            _base_offer('year', self._extract_result_year(base_result))
            _base_offer('developer', getattr(base_result, 'developer', ''))
            # description doesn't trigger the dialog on its own (has_options
            # below excludes it — see that comment), but once something else
            # already opened it, a candidate's own differing description
            # deserves the same zero-peer path year/developer just got: a
            # single itch.io hit with no same-tier peers was confirmed for
            # its name/developer, leaving its description entirely absent
            # from the picker even though the scrape clearly had one.
            _base_offer('description', getattr(base_result, 'description', ''))

            # Same zero-peer gap for the cover — additive like the per-peer
            # loop above (seen_images/model['images']), not exclusive like
            # the fields above: a confirmed candidate's own image never had
            # any path to reach model['images'] at all when there were no
            # same-tier peers to bring it in through that loop.
            _base_cover = (getattr(base_result, 'image_url', '') or '').strip()
            if _base_cover and _base_cover not in seen_images:
                seen_images.add(_base_cover)
                _ensure_base_source_meta()
                model['images'].append({'source': _bkey, 'value': _base_cover})

        # The name held right before this candidate was confirmed, offered
        # as a "go back" option. Name is applied UNCONDITIONALLY on confirm
        # (_apply_result_init never gates it on "currently empty" the way
        # description/developer/year are) — so unlike those fields, the
        # original value is already gone from the form by the time this
        # runs; pre_confirm_name is the only trace of it left. Without this,
        # confirming a messy-titled candidate (e.g. a forum thread's raw
        # post title) left no way back to a cleaner name a better source
        # had already set — not even a chip, since a candidate reached via
        # a different tier has no same-tier peer to offer one either.
        _pcn = (pre_confirm_name or '').strip()
        if (_pcn and _pcn.lower() != cur_name.lower()
                and not any(o['value'].lower() == _pcn.lower() for o in model['name'])):
            # Shaped like _peer_section_key's own "source · title · n" so
            # _source_header_row's src_id fallback (source.split(" · ")[0])
            # resolves to the localized label below instead of this raw key.
            _prev_label = t('add_game.merge_previous_value')
            _pkey = f"{_prev_label} · {_pcn} · 0"
            if _pkey not in model['source_meta']:
                model['source_meta'][_pkey] = {
                    'inspect_url': '',
                    'image_url': '',
                    'name': _pcn,
                    'source_id': _prev_label,
                    'label': _prev_label,
                }
            model['name'].append({'source': _pkey, 'value': _pcn})

        # description does NOT get to trigger the dialog on its own — a
        # differing description alone was never material enough to resurface
        # a candidate in the carousel either (see has_material above), so
        # it shouldn't be enough to pop the merge dialog by itself. Once
        # something else justifies showing it, the description chip is
        # still offered as one of the things to pick.
        model['has_options'] = any([
            model['name'], model['developer'], model['year'],
            model['images'], model['tags'], model['urls'], model['reviews'],
        ])
        return model

    def _apply_merge_selection(self, sel: dict):
        """Write ONLY the pieces the user picked in the merge preview."""
        if sel.get('name'):
            self._name_edit.setText(sel['name'])
        if sel.get('description'):
            self._desc_edit.setPlainText(sel['description'])
        if sel.get('developer'):
            self._developer_edit.setText(sel['developer'])
        if sel.get('year'):
            self._year_edit.setText(sel['year'])
        # Additive, like tags/urls below — each checked image is downloaded
        # and added to the carousel (_set_web_image never removes an
        # existing one), never a replacement of the current cover.
        for _img_url in sel.get('images', []) or []:
            self._download_and_set_image(_img_url)
        if sel.get('tags'):
            self._apply_web_tags(sel['tags'])
        if sel.get('reviews'):
            self._merge_reviews(sel['reviews'])
            for r in sel['reviews']:
                if isinstance(r, dict):
                    self._mark_source_applied(r.get('source') or '')
        _new_urls = [u for u in sel.get('urls', []) if u not in self._store_urls]
        if _new_urls:
            self._store_urls.extend(_new_urls)
            self._rebuild_url_chips()
            for u in _new_urls:
                self._mark_source_applied(self._source_from_url(u))
        if hasattr(self, '_rebuild_tag_chips'):
            self._rebuild_tag_chips()
        self._status_lbl.setText(t('add_game.data_saved'))
        fs = scaled(12, self)
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:{fs}px;")

    def _result_site_urls(self, result) -> list[str]:
        """All site URLs carried by a search result: the store page plus any
        extra site pages (e.g. the VNDB entry page)."""
        urls: list[str] = []
        u = (getattr(result, 'store_url', '') or '').strip()
        if u:
            urls.append(u)
        for eu in (getattr(result, 'extra_urls', None) or []):
            eu = (eu or '').strip()
            if eu and eu not in urls:
                urls.append(eu)
        return urls

    def _new_result_site_urls(self, result) -> list[str]:
        """The result's site URLs not yet present among the URL chips."""
        return [u for u in self._result_site_urls(result)
                if u not in (self._store_urls or [])]

    def _merge_result_urls(self, result) -> bool:
        """Append the result's new site URLs to the URL chips (union, never
        removes). Returns True when at least one URL was added."""
        new = self._new_result_site_urls(result)
        if not new:
            return False
        self._store_urls = list(self._store_urls or []) + new
        self._rebuild_url_chips()
        return True

    def _fetch_from_url_input(self):
        """Chain-icon entry point: open a small modal asking for the game
        page link, fetch its metadata (the URL is read directly — only VNDB
        and Steam links go through their API, falling back to the direct
        read if that fails), then route the result through the normal
        candidate-confirm flow — preview popup, tier rules and enrichment
        all behave as for a search result. A failed fetch stays inside the
        modal — with the failure REASON (anti-bot wall, unreachable page,
        no metadata) — so the user can correct the link and retry."""
        if self._has_shelvable_work():
            return
        # Must happen before ANY download this session, same as _web_search
        # — this entry point (paste-a-link) is exactly the one that used to
        # skip the capture entirely, since it never calls _web_search().
        self._capture_session_initial_image()
        prefill = (self._url_input.text().strip()
                   or (self._store_urls[0] if self._store_urls else ""))
        dlg = _UrlFetchDialog(self, prefill)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.info is not None:
            # A rejected link-fetch candidate must end quietly ("not
            # found"), never start the tier cascade a rejected API
            # result would.
            self._current_search_phase = 'generic'
            # Carousel shows only the just-fetched page, but same-tier
            # merge peers must still be searchable against whatever an
            # earlier search already found — otherwise _last_search_
            # candidates gets clobbered down to this one page and the
            # "keep what I already have" chip merge can never fire for a
            # direct-URL fetch (see _show_search_candidates docstring).
            _existing = list(getattr(self, '_last_search_candidates', None) or [])
            _pool = _existing + [dlg.info] if dlg.info not in _existing else _existing
            self._show_search_candidates([dlg.info], merge_pool=_pool)


class _UrlFetchDialog(QDialog):
    """Small modal for the chain-icon flow: paste a game page link, fetch
    its metadata in a background thread, close on success (the caller then
    opens the candidate preview). Failures stay INSIDE the modal with a
    specific reason — anti-bot wall (with the HTTP status), unreachable
    page, or page without usable metadata — so a stall or a protected page
    is never silently shown as a generic "not found".

    Thread hand-off uses a Qt Signal (queued to the GUI thread), NOT
    QTimer.singleShot from the worker: a plain threading.Thread has no Qt
    event loop, so a timer started there never fires and the dialog would
    hang on "Searching…" forever."""

    _fetch_done = Signal(object, object)   # (GameInfo|None, Exception|None)

    def __init__(self, parent, prefill: str = ""):
        super().__init__(parent)
        self.info = None   # set to the fetched GameInfo on accept
        self.setWindowTitle(t('add_game.fetch_url_title'))
        self.setWindowModality(Qt.WindowModality.WindowModal)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 14, 16, 14)

        intro = QLabel(t('add_game.fetch_url_msg'))
        intro.setWordWrap(True)
        intro.setObjectName("dialog_intro")
        lay.addWidget(intro)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText(t('add_game.store_url_placeholder'))
        self._edit.setText(prefill)
        lay.addWidget(self._edit)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setObjectName("dialog_status")
        lay.addWidget(self._status)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(t('common.cancel'))
        cancel_btn.setMinimumWidth(scaled(90, self))
        cancel_btn.clicked.connect(self.reject)
        self._fetch_btn = QPushButton(t('add_game.fetch_url_go'))
        self._fetch_btn.setObjectName("primary_btn")
        self._fetch_btn.setMinimumWidth(scaled(90, self))
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._fetch_btn)
        lay.addLayout(btn_row)

        self._fetch_btn.clicked.connect(self._start)
        self._edit.returnPressed.connect(self._start)
        self._fetch_done.connect(self._on_done)
        self._panel_size = finalize_adaptive_dialog_size(
            self, min_w=440, min_h=200)

    def _start(self):
        url = self._edit.text().strip()
        if not url:
            return
        self._fetch_btn.setEnabled(False)
        self._edit.setEnabled(False)
        self._status.setText(t('add_game.searching'))
        self._status.setStyleSheet("")  # restore #dialog_status

        def _bg(url=url):
            from core.game_api import fetch_info_from_url
            info = error = None
            try:
                info = fetch_info_from_url(url)
            except Exception as e:
                logger.debug(f"Link fetch failed for {url!r}: {e}")
                error = e
            try:
                self._fetch_done.emit(info, error)
            except RuntimeError:
                pass   # modal already closed

        threading.Thread(target=_bg, daemon=True).start()

    def _on_done(self, info, error):
        from core.game_api import UrlFetchError
        self._fetch_btn.setEnabled(True)
        self._edit.setEnabled(True)
        if info is not None and getattr(info, 'name', ''):
            self.info = info
            self.accept()
            return
        if isinstance(error, UrlFetchError) and error.kind == 'blocked':
            msg = t('add_game.fetch_url_blocked', code=error.status or '?')
        elif error is not None:
            msg = t('add_game.fetch_url_net_error')
            if getattr(error, 'status', 0):
                msg += f" (HTTP {error.status})"
        else:
            msg = t('add_game.search_not_found')
        self._status.setText(msg)
        self._status.setStyleSheet(f"color:{palette('warning')};font-size:{scaled(11, self)}px;")

