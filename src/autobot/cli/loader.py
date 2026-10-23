"""Reading a script: a YAML loader that rejects a key given twice, and the load errors as the CLI reports
them (see SPEC, CLI)."""

from __future__ import annotations

from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.reader import ReaderError

from ..log import ConsoleReporter
from .report import LoadError, visible

MERGE_TAG = "tag:yaml.org,2002:merge"
VALUE_TAG = "tag:yaml.org,2002:value"  # a plain `=` key, which flatten_mapping turns into the string "="
_MERGE = object()


class UniqueKeyLoader(yaml.SafeLoader):
    """`SafeLoader` that rejects a mapping key given twice instead of keeping the last value.

    Keys pulled in by a `<<` merge may be overridden; only keys written in the mapping itself must be unique.
    """

    def __init__(self, stream: object) -> None:
        super().__init__(stream)
        self._checked: set[yaml.Node] = set()

    # every mapping (merge sources too) is flattened before it's built, and flattening rewrites node.value,
    # so check the keys as written, once per node, before the first flatten
    def flatten_mapping(self, node: yaml.MappingNode) -> None:
        if node not in self._checked:
            self._checked.add(node)
            seen: dict[object, yaml.Node] = {}
            for k, _ in node.value:
                key = _MERGE if k.tag == MERGE_TAG else k.value if k.tag == VALUE_TAG else self.construct_object(k)
                try:
                    first = seen.setdefault(key, k)
                except TypeError:  # unhashable: construct_mapping reports it
                    continue
                if first is not k:
                    name = k.value if isinstance(k, yaml.ScalarNode) else key
                    raise ConstructorError("first defined", first.start_mark, f"found duplicate key {name!r}", k.start_mark)
        super().flatten_mapping(node)

    def construct_object(self, node: yaml.Node, deep: bool = False) -> Any:
        try:
            return super().construct_object(node, deep)
        except ValueError as e:
            # a scalar that reads as one of YAML's types and that Python's type can't hold: the date
            # 2001-99-99, an integer of 5000 digits. It is the document's error, at this node
            kind = node.tag.rpartition(":")[2]
            raise ConstructorError(
                None, None, f"invalid {kind} ({e}); quote the value if it is meant as text", node.start_mark
            ) from e


def load(rep: ConsoleReporter, path: str) -> object:
    """The document of the script at `path`. A file or YAML error is reported, and raised as `LoadError`."""
    name = visible(path)
    try:
        with open(path, "rb") as f:
            return yaml.load(f, Loader=UniqueKeyLoader)
    except OSError as e:
        rep.error(f"Cannot read script {name}", str(e.strerror or e))
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark
        at = f", line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        rep.error(f"YAML error in {name}{at}", str(e.problem))
        if e.context:
            mark = e.context_mark
            at = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
            rep.more(f"  {e.context}{at}")
    except ReaderError as e:
        what = "character" if e.encoding == "unicode" else f"{e.encoding} byte"
        rep.error(f"YAML error in {name}, position {e.position}", f"{e.reason} ({what} #x{e.character:02x})")
    except yaml.YAMLError as e:
        rep.error(f"YAML error in {name}", str(e))
    except RecursionError:
        # the parser follows the nesting of the document with its own: hundreds of levels are too many
        rep.error(f"YAML error in {name}", "the document is nested too deeply")
    except ValueError as e:  # from anywhere else in the loader: what the document holds, not a bug
        rep.error(f"YAML error in {name}", str(e) or type(e).__name__)
    raise LoadError
