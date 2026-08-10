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
from ui.styles.theme import palette
from ui.dialogs.search_enrichment import (CandidatePreviewDialog,
                                          EnrichmentMergeDialog,
                                          _inspect_url)

logger = logging.getLogger(__name__)


class SearchFlowMixin:
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

        exe_path = self._exe_edit.text().strip()

        appid = None
        from core.resolvers import is_launcher_url, get_appid_from_url
        if is_launcher_url(exe_path):
            appid = get_appid_from_url(exe_path)

        self._status_lbl.setText(status_msg or t('add_game.searching'))
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:12px;")
        self._search_progress.setVisible(True)

        # A new search invalidates any candidate popup from the previous one
        # (nothing to do here now — CandidatePreviewDialog is transient and
        # already closed by the time a new search can be triggered).

        # Disable save/search buttons during search
        self._add_btn.setEnabled(False)
        self._web_search_btn.setEnabled(False)

        # Store current state to check for overwrite
        self._original_name = self._name_edit.text().strip()
        self._original_desc = self._desc_edit.toPlainText().strip()
        self._original_image_path = self._image_path
        self._original_image_url = self._image_path_to_url.get(self._image_path) if self._image_path else None

        # Capture the image that existed before any search ran in this session (once only).
        # Used by reject/closeEvent to know which downloaded files to clean up on cancel.
        if not self._session_image_captured:
            self._session_initial_image_path = self._image_path
            self._session_image_captured = True

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

        # If the folder found via directory walking contains a DLsite product code
        # (e.g. "RJ01234567] Example Game v1.0.19b"), use ONLY the clean code.
        # Passing the raw folder name (with brackets, version strings, etc.) as a
        # search hint produces garbage secondary queries.
        if _folder_name:
            _folder_dl = _hint_re.search(r'(RJ|RE|VJ)(\d{4,10})', _folder_name, _hint_re.IGNORECASE)
            if _folder_dl:
                _folder_name = _folder_dl.group(0).upper()

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
            self.search_finished.emit(result, error)

        threading.Thread(target=do_search, daemon=True).start()

    def _on_search_finished(self, result, error):
        """Handle search completion."""
        self._search_progress.setVisible(False)
        if not self.isVisible() or self._cancel_event.is_set():
            self._add_btn.setEnabled(True)
            self._web_search_btn.setEnabled(bool(self._name_edit.text().strip()))
            return

        # The worker emits a best-first list of candidates (may be empty);
        # tolerate a single GameInfo/None for any legacy emitter.
        results = [r for r in (result if isinstance(result, list) else [result]) if r]


        self._status_lbl.setStyleSheet(f"color:{palette('text_secondary')};font-size:12px;")

        if error:
            self._add_btn.setEnabled(True)
            self._web_search_btn.setEnabled(True)
            self._status_lbl.setText(t('add_game.search_error', error=str(error)[:50]))
            self._status_lbl.setStyleSheet(f"color:{palette('error')};font-size:12px;")
            return

        _current_phase = getattr(self, '_current_search_phase', 'api')
        import logging as _log3
        _log3.getLogger(__name__).info(
            f"_on_search_finished: phase={_current_phase!r} "
            f"results={[r.name for r in results]!r}"
        )

        if not results:
            self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:12px;")
            self._add_btn.setEnabled(True)
            self._web_search_btn.setEnabled(True)
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
                    self._web_search(skip_primary_apis=True, enable_targeted_fallback=True,
                                     extra_folder_hint=_hint_fwd)
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
                    self._web_search(skip_primary_apis=True, enable_generic_fallback=True,
                                     extra_folder_hint=_hint_fwd)
            else:
                self._status_lbl.setText(
                    _rate_limited or t('add_game.search_not_found')
                )
            return

        # One distinct title or several: always review through the same
        # candidate-preview popup (‹ › browse when there's more than one,
        # arrows simply disabled for a single result) instead of silently
        # taking the first one. Drop sources already applied when they
        # carry no material news (a rewritten description alone is not news).
        self._invalidate_primary_reachability()
        self._show_search_candidates(results)

    def _source_label(self, raw_source: str) -> str:
        """Map an internal source id ('steam', 'web', 'itch+web'…) to its
        human label, preserving the '+ web' enrichment suffix."""
        from core.game_sources.common import source_label
        return source_label(raw_source)

    # ── Web-search candidate preview (single title or several) ───────────────

    def _show_search_candidates(self, results: list):
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
        """
        self._add_btn.setEnabled(True)
        self._web_search_btn.setEnabled(True)
        # Full set kept for same-tier merge peers; carousel only shows
        # candidates that would actually change something (or soft-promote
        # a dead primary). Re-offering Steam after a Steam save + VNDB
        # enrich with nothing new is exactly what this filters out.
        self._last_search_candidates = list(results)
        useful = [r for r in results
                  if self._compute_candidate_diff(r).get('has_changes')]
        if not useful:
            self._status_lbl.setText(t('add_game.candidate_no_changes'))
            self._status_lbl.setStyleSheet(
                f"color:{palette('text_secondary')};font-size:12px;")
            return
        n = len(useful)
        self._status_lbl.setText(
            t('add_game.candidates_found', n=n) if n > 1
            else t('add_game.candidate_found_single')
        )
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:12px;")
        while True:
            dlg = CandidatePreviewDialog(
                useful, self._compute_candidate_diff, self,
                extra_note=t('add_game.enrich_note') if n > 1 else '',
            )
            if dlg.exec() != QDialog.DialogCode.Accepted or dlg.selected is None:
                self._candidates_rejected()
                return
            snap = self._capture_search_form()
            if not self._process_search_result(dlg.selected, offer_enrichment=False):
                return
            if self._run_same_tier_merge(dlg.selected):
                self._restore_search_form(snap)
                continue
            return

    def _candidates_rejected(self):
        """No candidate confirmed (Reject / closed the popup): proceed
        straight to the next search tier, exactly like declining a single
        result — no extra "do you want to try the next tier?" prompt, the
        user already said no once by rejecting. Mirrors the decline branch
        in _process_search_result."""
        _current_phase = getattr(self, '_current_search_phase', 'api')
        _hint_fwd = getattr(self, '_last_search_folder_hint', '')
        self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:12px;")
        self._add_btn.setEnabled(True)
        self._web_search_btn.setEnabled(True)
        if _current_phase == 'api':
            self._status_lbl.setText(
                t('add_game.enrichment_step_status',
                  source=t('add_game.enrich_api'), status=t('add_game.search_not_found'))
            )
            self._web_search(skip_primary_apis=True, enable_targeted_fallback=True,
                             extra_folder_hint=_hint_fwd)
        elif _current_phase == 'targeted':
            self._status_lbl.setText(
                t('add_game.enrichment_step_status',
                  source=t('add_game.enrich_targeted'), status=t('add_game.search_not_found'))
            )
            self._web_search(skip_primary_apis=True, enable_generic_fallback=True,
                             extra_folder_hint=_hint_fwd)
        else:
            self._status_lbl.setText(t('add_game.search_not_found'))

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
            self._status_lbl.setStyleSheet(f"color:{palette('text_secondary')};font-size:12px;")
            self._add_btn.setEnabled(True)
            self._web_search_btn.setEnabled(True)
            self._status_lbl.setText(t('add_game.candidate_no_changes'))
            return False

        # ── Apply — the user already confirmed this exact candidate ───────
        # Destructive overwrite is no longer driven by "description looks
        # different" or a higher tier alone. A dead primary may be replaced
        # as the marker, but filled fields stay (enrich path).
        if not diff['has_existing']:
            self._apply_result_init(result)
        else:
            self._apply_result_enrich(result, diff['new_tags'])
        self._store_result_fingerprint(
            _raw_source, result, diff['result_year'],
            as_primary=bool(
                not diff['has_existing']
                or diff.get('promote_primary')
                or diff.get('same_origin')
            ),
        )

        self._status_lbl.setText(t('add_game.data_saved'))
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:12px;")
        self._add_btn.setEnabled(True)
        self._web_search_btn.setEnabled(True)

        if offer_enrichment:
            _peers = [r for r in (getattr(self, '_last_search_candidates', None) or [])
                      if r is not result]
            if _peers:
                self._offer_same_tier_enrichment(result, _peers)

        missing = self._get_missing_fields()
        if missing:
            self._status_lbl.setText(
                t('add_game.fields_still_missing', fields=", ".join(missing)))
            self._status_lbl.setStyleSheet(f"color:{palette('warning')};font-size:12px;")
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

        new_tags = [g for g in (result.genres or []) if g not in current_tags]
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

        is_overwrite = False
        same_origin_no_diff = bool(already_applied and not has_material
                                   and not promote_primary)

        # ── Per-field diff (for display — fill empty / rename only) ─────
        fields: dict = {}
        if name_change:
            fields['name'] = {'old': current_name or None, 'new': result.name}

        _rd = result.description or ''
        if _rd and not current_desc:
            fields['description'] = {'old': None, 'new': _rd}

        _rv = getattr(result, 'developer', '') or ''
        if _rv and not current_dev:
            fields['developer'] = {'old': None, 'new': _rv}

        if result_year and not current_year:
            fields['year'] = {'old': None, 'new': result_year}

        has_enrich = bool(has_material)
        has_changes = bool(has_material or promote_primary)

        return {
            'has_existing': has_existing,
            'is_overwrite': is_overwrite,
            'promote_primary': promote_primary,
            'same_origin': _is_same_origin,
            'same_origin_no_diff': same_origin_no_diff,
            'has_enrich': has_enrich,
            'has_changes': has_changes,
            'result_year': result_year,
            'fields': fields,
            'new_tags': new_tags,
            'new_urls': new_urls,
            'new_image': new_image,
            'new_reviews': new_reviews,
        }

    def _apply_result_init(self, result):
        """Case B — no existing data: fill all empty fields (union for
        tags). Name/image/genres are unconditional; description/developer/
        year only fill if currently empty (a field the user typed by hand
        even in an otherwise-blank form is never overwritten here)."""
        current_name = self._name_edit.text().strip()
        current_desc = self._desc_edit.toPlainText().strip()
        current_dev  = self._developer_edit.text().strip()
        current_year = self._year_edit.text().strip()
        if result.name and result.name != current_name:
            self._name_edit.setText(result.name)
        if result.image_url:
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
        if result.image_url:
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
        if result.image_url and not self._original_image_path:
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

    def _is_primary_source_reachable(self) -> bool:
        """Whether the saved primary's page still answers.

        Cached per search presentation. No URL for that source → assume
        reachable (cannot justify a soft promote). Probe failure → dead.
        """
        cached = getattr(self, '_primary_reachability_cache', None)
        if cached is not None:
            return cached
        fp = getattr(self, '_enrichment_source_fingerprint', None) or {}
        primary = (fp.get('source') or '').split('+')[0]
        if not primary:
            self._primary_reachability_cache = True
            return True
        urls = [u for u in (getattr(self, '_store_urls', None) or [])
                if self._source_from_url(u) == primary]
        if not urls:
            self._primary_reachability_cache = True
            return True
        ok = False
        for u in urls[:2]:
            if self._probe_page_reachable(u):
                ok = True
                break
        self._primary_reachability_cache = ok
        return ok

    @staticmethod
    def _probe_page_reachable(url: str, timeout: float = 4.0) -> bool:
        """Cheap GET: HTTP < 400 counts as alive. Network errors → dead."""
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

    def _same_tier_peers(self, base_result, others: list) -> list:
        """Peers for chip enrichment after a candidate is confirmed.

        Same *tier* only (no lower-tier search). The confirmed *source* is
        excluded entirely — picking Steam title 1 must not re-offer Steam
        title 2. Other sources keep every distinct title (VNDB 1 + VNDB 2).
        """
        _base_src = (getattr(base_result, 'source', '') or '').split('+')[0]
        _base_tier = self._get_source_tier(_base_src)
        peers = []
        for r in others:
            if r is base_result:
                continue
            src = (getattr(r, 'source', '') or '').split('+')[0]
            if src and _base_src and src == _base_src:
                continue               # same source already declared
            if self._get_source_tier(src) != _base_tier:
                continue
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

    def _run_same_tier_merge(self, base_result) -> bool:
        """Offer peer enrichment chips. Returns True when the user asked to
        go back to the candidate carousel (form snapshot must be restored)."""
        peers = self._same_tier_peers(
            base_result,
            getattr(self, '_last_search_candidates', None) or [],
        )
        if not peers:
            return False
        model = self._build_merge_model(peers)
        if not model.get('has_options'):
            return False
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
                    f"color:{palette('warning')};font-size:12px;")
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
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:12px;")

    def _build_merge_model(self, collected: list) -> dict:
        """Per-field option lists for the merge preview.

        The CONFIRMED candidate is authoritative: fields it filled are never
        offered for replacement — peers only compete for fields still EMPTY.
        Each peer title is its own section (``vndb::Title::0``), so two VNDB
        hits stay distinguishable. Tags/URLs expand additively; reviews are
        one chip per peer (same API source identity still collapses on apply).
        """
        cur_desc = self._desc_edit.toPlainText().strip()
        cur_dev  = self._developer_edit.text().strip()
        cur_year = self._year_edit.text().strip()
        cur_tags = {x.lower() for x in (getattr(self, '_tags', []) or [])}
        has_img  = bool(self._original_image_path or getattr(self, '_image_path', ''))

        peer_keys: list[str] = []
        for i, info in enumerate(collected):
            peer_keys.append(self._peer_section_key(info, i))

        def _opts(getter):
            opts, seen = [], set()
            for info, pkey in zip(collected, peer_keys):
                v = (getter(info) or '').strip()
                if not v or v.lower() in seen:
                    continue
                seen.add(v.lower())
                opts.append({'source': pkey, 'value': v})
            return opts

        model = {
            'current': {'description': cur_desc, 'developer': cur_dev,
                        'year': cur_year, 'has_image': has_img},
            'description': [] if cur_desc else _opts(lambda i: i.description),
            'developer':   [] if cur_dev  else _opts(lambda i: getattr(i, 'developer', '')),
            'year':        [] if cur_year else _opts(lambda i: self._extract_result_year(i)),
            'image':       [] if has_img  else _opts(lambda i: i.image_url),
            'tags': [],
            'urls': [],
            'reviews': [],
            'source_meta': {},
        }
        seen_tags = set(cur_tags)
        seen_urls: set[str] = set()
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
            for g in (info.genres or []):
                if g.lower() in seen_tags:
                    continue
                seen_tags.add(g.lower())
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
        model['has_options'] = any([
            model['description'], model['developer'], model['year'],
            model['image'], model['tags'], model['urls'], model['reviews'],
        ])
        return model

    def _apply_merge_selection(self, sel: dict):
        """Write ONLY the pieces the user picked in the merge preview."""
        if sel.get('description'):
            self._desc_edit.setPlainText(sel['description'])
        if sel.get('developer'):
            self._developer_edit.setText(sel['developer'])
        if sel.get('year'):
            self._year_edit.setText(sel['year'])
        if sel.get('image'):
            self._download_and_set_image(sel['image'])
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
        self._status_lbl.setStyleSheet(f"color:{palette('accent')};font-size:12px;")

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
        prefill = (self._url_input.text().strip()
                   or (self._store_urls[0] if self._store_urls else ""))
        dlg = _UrlFetchDialog(self, prefill)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.info is not None:
            # A rejected link-fetch candidate must end quietly ("not
            # found"), never start the tier cascade a rejected API
            # result would.
            self._current_search_phase = 'generic'
            self._show_search_candidates([dlg.info])


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
        self.setMinimumWidth(440)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        lay.setContentsMargins(16, 14, 16, 14)

        intro = QLabel(t('add_game.fetch_url_msg'))
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{palette('text_secondary')};font-size:12px;")
        lay.addWidget(intro)

        self._edit = QLineEdit()
        self._edit.setPlaceholderText(t('add_game.store_url_placeholder'))
        self._edit.setText(prefill)
        lay.addWidget(self._edit)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")
        lay.addWidget(self._status)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        cancel_btn = QPushButton(t('common.cancel'))
        cancel_btn.setMinimumWidth(90)
        cancel_btn.clicked.connect(self.reject)
        self._fetch_btn = QPushButton(t('add_game.fetch_url_go'))
        self._fetch_btn.setObjectName("primary_btn")
        self._fetch_btn.setMinimumWidth(90)
        btn_row.addWidget(cancel_btn)
        btn_row.addWidget(self._fetch_btn)
        lay.addLayout(btn_row)

        self._fetch_btn.clicked.connect(self._start)
        self._edit.returnPressed.connect(self._start)
        self._fetch_done.connect(self._on_done)

    def _start(self):
        url = self._edit.text().strip()
        if not url:
            return
        self._fetch_btn.setEnabled(False)
        self._edit.setEnabled(False)
        self._status.setText(t('add_game.searching'))
        self._status.setStyleSheet(f"color:{palette('text_muted')};font-size:11px;")

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
        self._status.setStyleSheet(f"color:{palette('warning')};font-size:11px;")

