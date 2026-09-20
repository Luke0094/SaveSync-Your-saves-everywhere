"""A save whose game locks it with a key derived from the player's own
SteamID64 — see core/save_editor/crypt/steamid_aes for the shared
decrypt/encrypt machinery and crypt/steamid_aes_recipes for the per-game
recipe list every instance of this reader is tried against. Generic across
every game listed there; nothing in THIS class is specific to any one of
them — see that module for what is.

Once decrypted the save is plain YAML, structurally the same shape JSON is
(dict / list / scalar), so this reuses _walk exactly the way JsonFormat
does rather than writing a second tree-walker.

``name``/``engine`` start as a generic description and are replaced, per
instance, by whichever recipe actually matched, the moment load() succeeds
— save_editor.make_doc reads them off the instance for exactly this reason
(see StructHeaderFormat for the same convention, one save format family
over).

Re-serialised, not byte-exact (verify_exact=False, proven by value round
trip like JsonFormat) — matching the community tools each recipe here was
cross-checked against, which likewise re-dump plain YAML rather than
reproducing the original formatting. Custom ``!``-tagged nodes are read as
their plain underlying value and written back untagged: the same
simplification those tools make, and evidently one the game's own loader
tolerates — nothing here invents a lossless tag round trip unverified
against a real save.
"""
from .base import SaveEditorError, _Format, _walk

# Built once, the first time a save like this is actually opened — the
# `import yaml` it needs stays out of module import time, same as every
# other non-stdlib dependency in this package (cryptography, numba, ...).
_loader_cls = None


def _tag_stripped(loader, _tag_suffix, node):
    """A ``!``-tagged node, read as its plain value — see module docstring
    for why the tag itself is not kept."""
    import yaml
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return None


def _get_loader():
    global _loader_cls
    if _loader_cls is None:
        import yaml

        class _TagStrippingLoader(yaml.SafeLoader):
            pass

        _TagStrippingLoader.add_multi_constructor('!', _tag_stripped)
        _loader_cls = _TagStrippingLoader
    return _loader_cls


def _find_recipe(path):
    """The one recipe (see crypt/steamid_aes_recipes) whose game this path
    sits under, or None."""
    from core.save_editor.crypt.steamid_aes_recipes import RECIPES
    haystack = str(path).lower()
    for recipe in RECIPES:
        if recipe.folder_marker.lower() in haystack:
            return recipe
    return None


class SteamIdAesFormat(_Format):
    name = "SteamID-keyed save"
    engine = "SteamID-keyed save"
    verify_exact = False

    def __init__(self):
        self.data = None
        self.source_path = None
        self._recipe = None
        self._steamid = ""

    def load(self, data: bytes) -> None:
        import yaml
        from core.save_editor.crypt.steamid_aes import find_save_plaintext

        recipe = _find_recipe(self.source_path) if self.source_path else None
        if recipe is None:
            raise SaveEditorError(
                "this file does not sit under a game this reader knows a "
                "SteamID-key recipe for")
        steamid, plain = find_save_plaintext(data, recipe, self.source_path)
        if not steamid:
            raise SaveEditorError(
                f"this {recipe.name} save needs the owning SteamID64 to "
                f"decrypt, and none tried (from the save's own path, or "
                f"from a Steam account on this machine) opened it")
        try:
            parsed = yaml.load(plain.decode("utf-8"), Loader=_get_loader())
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            raise SaveEditorError(
                f"decrypted, but the result will not read as YAML: {e}") from e
        if not isinstance(parsed, (dict, list)):
            raise SaveEditorError(
                "decrypted, but the result holds no editable structure")
        self.data = parsed
        self._recipe = recipe
        self._steamid = steamid
        self.name = recipe.name
        self.engine = recipe.name

    def dump(self) -> bytes:
        import yaml
        from core.save_editor.crypt.steamid_aes import encrypt
        # sort_keys=False: PyYAML's own default (True) reorders every
        # mapping's keys alphabetically on dump, which does not change what
        # the save MEANS (a YAML mapping's key order carries no meaning) but
        # does change the order fields() reports them in — and the open
        # gate's round-trip check compares that as an ordered list. Any
        # save whose keys are not already alphabetical would dump correct
        # VALUES under keys in a different ORDER, reads back as "differs",
        # and gets demoted to read-only despite nothing actually being
        # wrong — confirmed directly: a two-key {level, gold} test dumped
        # as {gold, level} and failed verification on order alone.
        plain = yaml.safe_dump(
            self.data, default_flow_style=False, allow_unicode=True,
            sort_keys=False,
        ).encode("utf-8")
        return encrypt(plain, self._steamid, self._recipe.base_key)

    def fields(self) -> list:
        return _walk(self.data)

    def set_field(self, path: tuple, value) -> None:
        node = self.data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
