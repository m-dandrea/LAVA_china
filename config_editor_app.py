"""Streamlit-based editor for LAVA China configuration templates."""

from __future__ import annotations

import copy
import difflib
import io
import numbers
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

import streamlit as st
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import ScalarString

REPO_ROOT = Path(__file__).resolve().parent
CONFIG_DIR = REPO_ROOT / "configs"
CONFIG_FILES = {
    "General template (config_template_china.yaml)": CONFIG_DIR / "config_template_china.yaml",
    "Onshore wind template (onshorewind_template_china.yaml)": CONFIG_DIR / "onshorewind_template_china.yaml",
    "Solar template (solar_template_china.yaml)": CONFIG_DIR / "solar_template_china.yaml",
}


def _initialise_yaml() -> tuple[YAML, YAML, YAML]:
    loader = YAML(typ="rt")
    dumper = YAML(typ="rt")
    safe_loader = YAML(typ="safe")
    for yaml_obj in (loader, dumper):
        yaml_obj.preserve_quotes = True
        yaml_obj.indent(mapping=2, sequence=4, offset=2)
        yaml_obj.width = 4096
    return loader, dumper, safe_loader


YAML_LOADER, YAML_DUMPER, YAML_SAFE = _initialise_yaml()

MAPPING_TYPES = (CommentedMap, dict)
SEQUENCE_TYPES = (CommentedSeq, list)


def load_config(path: Path) -> CommentedMap:
    with path.open("r", encoding="utf-8") as stream:
        data = YAML_LOADER.load(stream)
    if not isinstance(data, CommentedMap):
        raise TypeError(f"Expected mapping at root of {path.name}")
    return data


def save_config(path: Path, data: CommentedMap) -> None:
    with path.open("w", encoding="utf-8") as stream:
        YAML_DUMPER.dump(data, stream)


def dump_yaml_to_string(data: Any) -> str:
    buffer = io.StringIO()
    YAML_DUMPER.dump(data, buffer)
    return buffer.getvalue()


def calc_text_area_height(text: str) -> int:
    line_count = text.count("\n") + 1
    return max(160, min(600, line_count * 24))


def make_widget_key(path: Iterable[str], suffix: str | None = None) -> str:
    parts = [str(part).replace(" ", "_") for part in path]
    base = "__".join(parts) if parts else "root"
    if suffix:
        base = f"{base}__{suffix}"
    prefix = st.session_state.get("_widget_prefix", "")
    return f"{prefix}{base}"


def make_help_text(path: Iterable[str]) -> str | None:
    parts = [str(part) for part in path]
    if not parts:
        return None
    return " → ".join(parts)


def reset_widget_state() -> None:
    prefix = st.session_state.get("_widget_prefix")
    if prefix:
        keys_to_delete = [key for key in list(st.session_state.keys()) if isinstance(key, str) and key.startswith(prefix)]
        for key in keys_to_delete:
            del st.session_state[key]
    st.session_state["_widget_prefix"] = f"{uuid4().hex}__"


def ensure_file_loaded(path: Path) -> None:
    if "_config_state" not in st.session_state:
        st.session_state["_config_state"] = {}
    path = path.resolve()
    path_key = str(path)
    state = st.session_state["_config_state"].get(path_key)
    if state is None:
        data = load_config(path)
        state = {
            "path": path,
            "data": data,
            "saved": copy.deepcopy(data),
        }
        st.session_state["_config_state"][path_key] = state
    st.session_state["_active_file"] = path_key
    st.session_state["_config_data"] = state["data"]
    reset_widget_state()


def get_current_state() -> dict[str, Any]:
    path_key = st.session_state.get("_active_file")
    if not path_key:
        raise RuntimeError("No active configuration file selected")
    return st.session_state["_config_state"][path_key]


def reload_from_disk(path: Path) -> None:
    path = path.resolve()
    data = load_config(path)
    state = {
        "path": path,
        "data": data,
        "saved": copy.deepcopy(data),
    }
    st.session_state["_config_state"][str(path)] = state
    st.session_state["_config_data"] = state["data"]
    st.session_state["_active_file"] = str(path)
    reset_widget_state()


def revert_to_last_saved() -> None:
    state = get_current_state()
    restored = copy.deepcopy(state["saved"])
    state["data"] = restored
    st.session_state["_config_data"] = restored
    reset_widget_state()


def has_unsaved_changes() -> bool:
    state = get_current_state()
    current_text = dump_yaml_to_string(state["data"])
    saved_text = dump_yaml_to_string(state["saved"])
    return current_text != saved_text


def should_render(path: tuple[str, ...], value: Any, search_term: str) -> bool:
    if not search_term:
        return True
    search = search_term.lower()
    path_text = " ".join(path).lower()
    if search in path_text:
        return True
    if isinstance(value, str):
        return search in value.lower()
    if isinstance(value, bool):
        return search in str(value).lower()
    if isinstance(value, numbers.Number):
        return search in str(value).lower()
    if isinstance(value, MAPPING_TYPES):
        for key, subvalue in value.items():
            if should_render(path + (str(key),), subvalue, search_term):
                return True
        return False
    if isinstance(value, SEQUENCE_TYPES):
        for idx, item in enumerate(value):
            if should_render(path + (f"[{idx}]",), item, search_term):
                return True
        try:
            text = dump_yaml_to_string(value)
        except Exception:
            return False
        return search in text.lower()
    return False


def convert_text_to_value(text: str, original: Any) -> Any:
    stripped = text.strip()
    if stripped.lower() in {"null", "none", "~"}:
        return None
    if isinstance(original, bool):
        return stripped.lower() in {"true", "1", "yes"}
    if isinstance(original, numbers.Integral) and not isinstance(original, bool):
        if stripped == "":
            raise ValueError("Value required")
        return type(original)(int(stripped))
    if isinstance(original, numbers.Real) and not isinstance(original, numbers.Integral):
        if stripped == "":
            raise ValueError("Value required")
        return type(original)(float(stripped))
    if stripped == "" and original is None:
        return None
    if original is None:
        try:
            parsed = YAML_SAFE.load(stripped)
        except Exception:
            return stripped
        return parsed
    if isinstance(original, ScalarString):
        return original.__class__(text)
    if isinstance(original, str):
        return text
    return stripped if stripped else text


def render_scalar(label: str, value: Any, path: tuple[str, ...]) -> Any:
    if isinstance(value, bool):
        key = make_widget_key(path)
        return st.checkbox(label, value=value, key=key, help=make_help_text(path))
    display = "" if value is None else str(value)
    key = make_widget_key(path)
    new_text = st.text_input(label, value=display, key=key, help=make_help_text(path))
    try:
        return convert_text_to_value(new_text, value)
    except ValueError as exc:
        st.warning(f"{label}: {exc}")
        return value


def render_sequence(label: str, value: Any, path: tuple[str, ...]) -> Any:
    key = make_widget_key(path)
    initial_text = dump_yaml_to_string(value).rstrip("\n")
    text = st.text_area(
        label,
        value=initial_text,
        key=key,
        help=make_help_text(path),
        height=calc_text_area_height(initial_text),
    )
    try:
        parsed = YAML_LOADER.load(text) if text.strip() else []
    except Exception as exc:
        st.error(f"Unable to parse list for {label}: {exc}")
        return value
    if parsed is None:
        parsed = []
    if not isinstance(parsed, list):
        st.error(f"{label} must be a YAML list")
        return value
    if isinstance(value, CommentedSeq):
        value.clear()
        value.extend(parsed)
        return value
    return parsed


def render_mapping(mapping: CommentedMap, path: tuple[str, ...], search_term: str) -> bool:
    rendered_any = False
    for key in list(mapping.keys()):
        subpath = path + (str(key),)
        value = mapping[key]
        if not should_render(subpath, value, search_term):
            continue
        rendered_any = True
        label = str(key)
        if isinstance(value, MAPPING_TYPES):
            expanded = bool(search_term)
            with st.expander(label, expanded=expanded):
                toggle_key = make_widget_key(subpath, "yaml_mode")
                yaml_mode = st.checkbox("Edit entire section as YAML", value=False, key=toggle_key)
                if yaml_mode:
                    updated = render_yaml_block(label, value, subpath)
                    if updated is not value:
                        mapping[key] = updated
                else:
                    child_rendered = render_mapping(value, subpath, search_term)
                    if search_term and not child_rendered:
                        st.info("No entries match the active filter in this section.")
        elif isinstance(value, SEQUENCE_TYPES):
            updated = render_sequence(label, value, subpath)
            if updated is not value:
                mapping[key] = updated
        else:
            mapping[key] = render_scalar(label, value, subpath)
    return rendered_any


def render_yaml_block(label: str, value: Any, path: tuple[str, ...]) -> Any:
    key = make_widget_key(path, "yaml_block")
    initial_text = dump_yaml_to_string(value).rstrip("\n")
    text = st.text_area(
        f"YAML editor for {label}",
        value=initial_text,
        key=key,
        height=calc_text_area_height(initial_text),
        help="Edit the raw YAML for this section, including keys.",
    )
    try:
        parsed = YAML_LOADER.load(text) if text.strip() else CommentedMap()
    except Exception as exc:
        st.error(f"Unable to parse YAML for {label}: {exc}")
        return value
    if parsed is None:
        parsed = CommentedMap()
    if not isinstance(parsed, MAPPING_TYPES):
        st.error(f"{label} must be a YAML mapping")
        return value
    return parsed


def show_sidebar(path: Path, search_term: str) -> None:
    st.sidebar.header("File actions")
    if st.sidebar.button("Reload from disk"):
        reload_from_disk(path)
        st.sidebar.success("Configuration reloaded from disk.")
    if st.sidebar.button("Revert unsaved changes"):
        revert_to_last_saved()
        st.sidebar.info("Reverted to the last saved state.")
    if st.sidebar.button("Save changes", type="primary"):
        state = get_current_state()
        save_config(path, state["data"])
        state["saved"] = copy.deepcopy(state["data"])
        st.sidebar.success("Saved changes to disk.")
    state = get_current_state()
    if has_unsaved_changes():
        st.sidebar.warning("There are unsaved changes.")
        with st.sidebar.expander("Preview YAML diff"):
            current_text = dump_yaml_to_string(state["data"]).splitlines()
            saved_text = dump_yaml_to_string(state["saved"]).splitlines()
            diff = "\n".join(
                difflib.unified_diff(saved_text, current_text, fromfile="saved", tofile="current", lineterm="")
            )
            if diff.strip():
                st.code(diff, language="diff")
            else:
                st.write("No diff available.")
    yaml_text = dump_yaml_to_string(state["data"])
    st.sidebar.download_button(
        label="Download YAML",
        data=yaml_text,
        file_name=path.name,
        mime="text/yaml",
    )
    st.sidebar.markdown("---")
    st.sidebar.subheader("Filter options")
    st.sidebar.caption("Show only parameters whose name or value matches the filter.")
    st.sidebar.text_input("Filter", value=search_term, key="_search_term")


def main() -> None:
    st.set_page_config(page_title="LAVA China configuration editor", layout="wide")
    st.title("LAVA China configuration editor")
    st.markdown(
        "Use this interface to edit the configuration templates. "
        "Changes are kept in memory until you click **Save changes**."
    )
    if "_widget_prefix" not in st.session_state:
        st.session_state["_widget_prefix"] = f"{uuid4().hex}__"
    st.sidebar.title("Configuration files")
    available_options = []
    for label, path in CONFIG_FILES.items():
        if path.exists():
            available_options.append(label)
        else:
            st.sidebar.error(f"Missing file: {path.relative_to(REPO_ROOT)}")
    if not available_options:
        st.error("No configuration files found.")
        return
    default_label = available_options[0]
    selected_label = st.sidebar.selectbox("Choose a file", available_options, index=available_options.index(default_label))
    config_path = CONFIG_FILES[selected_label]
    ensure_file_loaded(config_path)
    search_term = st.session_state.get("_search_term", "")
    show_sidebar(config_path, search_term)
    config_data = st.session_state.get("_config_data")
    if config_data is None:
        st.error("Configuration data could not be loaded.")
        return
    st.markdown(f"### Editing `{config_path.relative_to(REPO_ROOT)}`")
    st.caption("Hover over a field label to see its full location path within the YAML structure.")
    rendered = render_mapping(config_data, tuple(), search_term)
    if not rendered:
        st.info("No configuration entries match the current filter.")


if __name__ == "__main__":
    main()
