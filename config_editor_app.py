"""Tkinter-based editor for LAVA China configuration templates."""

from __future__ import annotations

import copy
import difflib
import io
import numbers
import queue
import shlex
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText
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

SNAKEMAKE_SNAKEFILE = REPO_ROOT / "snakemake" / "Snakefile_short_short"

DEFAULT_SNAKEMAKE_COMMAND = f"snakemake --snakefile {SNAKEMAKE_SNAKEFILE} --cores 1"

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
PathElement = str | int


@dataclass
class ConfigState:
    path: Path
    data: CommentedMap
    saved: CommentedMap


@dataclass
class NodeRef:
    parent: Any | None
    key: Any | None
    value: Any
    path: tuple[PathElement, ...]


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

def convert_text_to_value(text: str, original: Any) -> Any:
    stripped = text.strip()
    if stripped.lower() in {"null", "none", "~"}:
        return None
    if isinstance(original, bool):
        return stripped.lower() in {"true", "1", "yes", "on"}
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


class ConfigEditorApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("LAVA China configuration editor")
        self.root.minsize(960, 600)
        self.states: dict[str, ConfigState] = {}
        self.current_state: ConfigState | None = None
        self.current_label: str | None = None
        self.selected_node: NodeRef | None = None
        self.node_index: dict[str, NodeRef] = {}
        self.path_index: dict[tuple[PathElement, ...], str] = {}
        self.snakemake_thread: threading.Thread | None = None
        self.snakemake_queue: queue.Queue[tuple[str, Any]] | None = None
        self.snakemake_dialog: tk.Toplevel | None = None
        self.snakemake_text: ScrolledText | None = None
        self.snakemake_close_button: ttk.Button | None = None
        self.snakemake_running = False
        self.snakemake_command_var = tk.StringVar(value=DEFAULT_SNAKEMAKE_COMMAND)
        self._create_widgets()
        self._populate_file_choices()
        self._load_initial_file()

    # ------------------------------------------------------------------
    # GUI setup helpers
    def _create_widgets(self) -> None:
        self.root.columnconfigure(0, weight=0)
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(0, weight=1)

        self.sidebar = ttk.Frame(self.root, padding=(12, 10))
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.rowconfigure(13, weight=1)

        ttk.Label(self.sidebar, text="Configuration file", font=("TkDefaultFont", 11, "bold")).grid(
            row=0, column=0, sticky="w"
        )
        self.file_var = tk.StringVar()
        self.file_combo = ttk.Combobox(
            self.sidebar,
            textvariable=self.file_var,
            state="readonly",
            values=list(CONFIG_FILES.keys()),
        )
        self.file_combo.grid(row=1, column=0, sticky="ew", pady=(4, 10))
        self.file_combo.bind("<<ComboboxSelected>>", self._on_file_selected)

        self.reload_button = ttk.Button(self.sidebar, text="Reload from disk", command=self.reload_from_disk)
        self.reload_button.grid(row=2, column=0, sticky="ew")
        self.revert_button = ttk.Button(
            self.sidebar, text="Revert unsaved changes", command=self.revert_to_last_saved
        )
        self.revert_button.grid(row=3, column=0, sticky="ew", pady=(4, 0))
        self.save_button = ttk.Button(self.sidebar, text="Save changes", command=self.save_changes)
        self.save_button.grid(row=4, column=0, sticky="ew", pady=(12, 0))
        self.diff_button = ttk.Button(self.sidebar, text="Show YAML diff", command=self.show_diff)
        self.diff_button.grid(row=5, column=0, sticky="ew", pady=(4, 0))
        self.run_snakemake_button = ttk.Button(
            self.sidebar, text="Run Snakemake workflow", command=self.run_snakemake_workflow
        )
        self.run_snakemake_button.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(self.sidebar, text="Snakemake command").grid(row=7, column=0, sticky="w", pady=(8, 0))
        self.snakemake_command_entry = ttk.Entry(
            self.sidebar, textvariable=self.snakemake_command_var
        )
        self.snakemake_command_entry.grid(row=8, column=0, sticky="ew")

        ttk.Separator(self.sidebar).grid(row=9, column=0, sticky="ew", pady=12)

        ttk.Label(self.sidebar, text="Filter parameters", font=("TkDefaultFont", 11, "bold")).grid(
            row=10, column=0, sticky="w"
        )
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.rebuild_tree())
        self.search_entry = ttk.Entry(self.sidebar, textvariable=self.search_var)
        self.search_entry.grid(row=11, column=0, sticky="ew", pady=(4, 0))
        self.clear_filter_button = ttk.Button(
            self.sidebar, text="Clear filter", command=lambda: self.search_var.set("")
        )
        self.clear_filter_button.grid(row=12, column=0, sticky="ew", pady=(4, 0))
        self.unsaved_var = tk.StringVar()
        self.unsaved_label = ttk.Label(
            self.sidebar, textvariable=self.unsaved_var, foreground="#b35c00", wraplength=220
        )

        self.unsaved_label.grid(row=13, column=0, sticky="sw", pady=(16, 0))
        self.main_frame = ttk.Frame(self.root, padding=(0, 10, 12, 10))
        self.main_frame.grid(row=0, column=1, sticky="nsew")
        self.main_frame.columnconfigure(0, weight=1)
        self.main_frame.rowconfigure(0, weight=1)

        self.main_pane = ttk.PanedWindow(self.main_frame, orient="vertical")
        self.main_pane.grid(row=0, column=0, sticky="nsew")

        tree_container = ttk.Frame(self.main_pane)
        tree_container.columnconfigure(0, weight=1)
        tree_container.rowconfigure(0, weight=1)

        columns = ("value", "type")
        self.tree = ttk.Treeview(tree_container, columns=columns, show="tree headings", selectmode="browse")
        self.tree.heading("#0", text="Parameter")
        self.tree.heading("value", text="Value")
        self.tree.heading("type", text="Type")
        self.tree.column("#0", width=260, anchor="w")
        self.tree.column("value", width=320, anchor="w")
        self.tree.column("type", width=120, anchor="w")

        y_scroll = ttk.Scrollbar(tree_container, orient="vertical", command=self.tree.yview)
        x_scroll = ttk.Scrollbar(tree_container, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")

        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)

        self.main_pane.add(tree_container, weight=3)

        detail_frame = ttk.LabelFrame(self.main_pane, text="Selected parameter", padding=(12, 10))
        detail_frame.columnconfigure(1, weight=1)

        ttk.Label(detail_frame, text="Path:").grid(row=0, column=0, sticky="nw")
        self.path_var = tk.StringVar(value="Select a parameter from the tree.")
        self.path_label = ttk.Label(detail_frame, textvariable=self.path_var, wraplength=520)
        self.path_label.grid(row=0, column=1, sticky="w")

        ttk.Label(detail_frame, text="Type:").grid(row=1, column=0, sticky="nw", pady=(4, 0))
        self.type_var = tk.StringVar(value="—")
        self.type_label = ttk.Label(detail_frame, textvariable=self.type_var)
        self.type_label.grid(row=1, column=1, sticky="w", pady=(4, 0))

        self.value_container = ttk.Frame(detail_frame)
        self.value_container.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        self.value_container.columnconfigure(0, weight=1)

        self.detail_frames: list[ttk.Frame] = []
        self._build_detail_frames()
        self._show_detail_frame(self.no_selection_frame)

        button_row = ttk.Frame(detail_frame)
        button_row.grid(row=3, column=0, columnspan=2, sticky="w", pady=(12, 0))
        self.yaml_button = ttk.Button(button_row, text="Edit value as YAML…", command=self.open_yaml_editor)
        self.yaml_button.grid(row=0, column=0, padx=(0, 8))
        self.copy_path_button = ttk.Button(button_row, text="Copy path", command=self.copy_path_to_clipboard)
        self.copy_path_button.grid(row=0, column=1)
        self.yaml_button.state(["disabled"])
        self.copy_path_button.state(["disabled"])

        self.status_var = tk.StringVar(value="Ready")
        status_bar = ttk.Label(detail_frame, textvariable=self.status_var, anchor="w")
        status_bar.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(12, 0))

        self.main_pane.add(detail_frame, weight=2)

    def _build_detail_frames(self) -> None:
        self.no_selection_frame = ttk.Frame(self.value_container)
        ttk.Label(self.no_selection_frame, text="Select a parameter in the tree to edit its value.").grid(
            row=0, column=0, sticky="w"
        )
        self.no_selection_frame.grid(row=0, column=0, sticky="w")

        self.scalar_frame = ttk.Frame(self.value_container)
        ttk.Label(self.scalar_frame, text="Enter a new value and click Apply.").grid(
            row=0, column=0, sticky="w", pady=(0, 6)
        )
        self.scalar_var = tk.StringVar()
        self.scalar_entry = ttk.Entry(self.scalar_frame, textvariable=self.scalar_var, width=50)
        self.scalar_entry.grid(row=1, column=0, sticky="w")
        self.scalar_apply = ttk.Button(self.scalar_frame, text="Apply change", command=self.apply_scalar_change)
        self.scalar_apply.grid(row=1, column=1, sticky="w", padx=(8, 0))

        self.bool_frame = ttk.Frame(self.value_container)
        self.bool_var = tk.BooleanVar()
        self.bool_check = ttk.Checkbutton(self.bool_frame, text="Value", variable=self.bool_var)
        self.bool_check.grid(row=0, column=0, sticky="w")
        self.bool_apply = ttk.Button(self.bool_frame, text="Apply change", command=self.apply_bool_change)
        self.bool_apply.grid(row=0, column=1, sticky="w", padx=(8, 0))

        self.complex_frame = ttk.Frame(self.value_container)
        ttk.Label(
            self.complex_frame,
            text="Lists and nested sections can be edited through the YAML editor.",
            wraplength=520,
        ).grid(row=0, column=0, sticky="w")

        self.detail_frames.extend(
            [self.no_selection_frame, self.scalar_frame, self.bool_frame, self.complex_frame]
        )
        for frame in self.detail_frames:
            frame.grid(row=0, column=0, sticky="w")
            frame.grid_remove()

    # ------------------------------------------------------------------
    # State handling
    def _populate_file_choices(self) -> None:
        for label, path in CONFIG_FILES.items():
            if not path.exists():
                messagebox.showerror("Missing file", f"Could not find {path.relative_to(REPO_ROOT)}")

    def _load_initial_file(self) -> None:
        available = [label for label, path in CONFIG_FILES.items() if path.exists()]
        if not available:
            self.set_status("No configuration files found.")
            return
        first_label = available[0]
        self.file_combo.set(first_label)
        self.load_configuration(first_label)

    def _on_file_selected(self, event: tk.Event) -> None:
        label = self.file_var.get()
        if label:
            self.load_configuration(label)

    def load_configuration(self, label: str) -> None:
        path = CONFIG_FILES[label]
        state = self._ensure_state(path)
        self.current_state = state
        self.current_label = label
        self.selected_node = None
        self.rebuild_tree()
        self._show_detail_frame(self.no_selection_frame)
        self.path_var.set("Select a parameter from the tree.")
        self.type_var.set("—")
        self.yaml_button.state(["disabled"])
        self.copy_path_button.state(["disabled"])
        self.set_status(f"Loaded {path.name}")

    def _ensure_state(self, path: Path) -> ConfigState:
        path = path.resolve()
        key = str(path)
        state = self.states.get(key)
        if state is None:
            data = load_config(path)
            state = ConfigState(path=path, data=data, saved=copy.deepcopy(data))
            self.states[key] = state
        return state

    def reload_from_disk(self) -> None:
        if not self.current_label:
            return
        path = CONFIG_FILES[self.current_label]
        try:
            data = load_config(path)
        except Exception as exc:
            messagebox.showerror("Reload failed", f"Could not load {path.name}: {exc}")
            return
        state = ConfigState(path=path.resolve(), data=data, saved=copy.deepcopy(data))
        self.states[str(path.resolve())] = state
        self.current_state = state
        self.rebuild_tree()
        self.set_status(f"Reloaded {path.name}")

    def revert_to_last_saved(self) -> None:
        state = self.current_state
        if state is None:
            return
        state.data = copy.deepcopy(state.saved)
        self.rebuild_tree()
        self.set_status("Reverted to last saved version.")

    def save_changes(self) -> None:
        state = self.current_state
        if state is None:
            return
        try:
            save_config(state.path, state.data)
        except Exception as exc:
            messagebox.showerror("Save failed", f"Could not save {state.path.name}: {exc}")
            return
        state.saved = copy.deepcopy(state.data)
        self.update_title()
        self.update_action_states()
        self.set_status(f"Saved changes to {state.path.name}")

    def has_unsaved_changes(self) -> bool:
        state = self.current_state
        if state is None:
            return False
        current_text = dump_yaml_to_string(state.data)
        saved_text = dump_yaml_to_string(state.saved)
        return current_text != saved_text

    # ------------------------------------------------------------------
    # Tree and selection handling
    def rebuild_tree(self, select_path: tuple[PathElement, ...] | None = None) -> None:
        state = self.current_state
        if state is None:
            self.tree.delete(*self.tree.get_children())
            return
        if select_path is None and self.selected_node is not None:
            select_path = self.selected_node.path

        filter_text = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self.node_index.clear()
        self.path_index.clear()

        for key, value in state.data.items():
            node = NodeRef(parent=state.data, key=key, value=value, path=(key,))
            self._add_tree_item("", node, filter_text)

        self.update_title()
        self.update_action_states()

        if select_path:
            self._select_path(select_path)

    def _add_tree_item(self, parent_id: str, node: NodeRef, filter_text: str) -> bool:
        match_self = self._node_matches(node.path, node.value, filter_text)
        item_id = self.tree.insert(
            parent_id,
            "end",
            text=self._node_label(node.path[-1]),
            values=(self._format_value(node.value), self._describe_type(node.value)),
        )
        if filter_text:
            self.tree.item(item_id, open=True)
        self.node_index[item_id] = node
        self.path_index[node.path] = item_id

        children_added = False
        if isinstance(node.value, MAPPING_TYPES):
            for child_key, child_value in node.value.items():
                child_node = NodeRef(
                    parent=node.value,
                    key=child_key,
                    value=child_value,
                    path=node.path + (child_key,),
                )
                if self._add_tree_item(item_id, child_node, filter_text):
                    children_added = True
        elif isinstance(node.value, SEQUENCE_TYPES):
            for idx, child_value in enumerate(node.value):
                child_node = NodeRef(
                    parent=node.value,
                    key=idx,
                    value=child_value,
                    path=node.path + (idx,),
                )
                if self._add_tree_item(item_id, child_node, filter_text):
                    children_added = True

        if not match_self and not children_added:
            self.tree.delete(item_id)
            self.node_index.pop(item_id, None)
            self.path_index.pop(node.path, None)
            return False
        return True

    def _node_label(self, key: PathElement) -> str:
        if isinstance(key, int):
            return f"[{key}]"
        return str(key)

    def _format_value(self, value: Any) -> str:
        if isinstance(value, MAPPING_TYPES):
            try:
                size = len(value)  # type: ignore[arg-type]
            except Exception:
                size = "?"
            return f"Mapping ({size})"
        if isinstance(value, SEQUENCE_TYPES):
            return f"List ({len(value)})"
        if value is None:
            return "null"
        return str(value)

    def _describe_type(self, value: Any) -> str:
        if isinstance(value, MAPPING_TYPES):
            return "Mapping"
        if isinstance(value, SEQUENCE_TYPES):
            return "Sequence"
        if isinstance(value, bool):
            return "Boolean"
        if isinstance(value, numbers.Integral):
            return "Integer"
        if isinstance(value, numbers.Real):
            return "Float"
        if isinstance(value, str):
            return "String"
        if value is None:
            return "Null"
        return type(value).__name__

    def _node_matches(self, path: tuple[PathElement, ...], value: Any, filter_text: str) -> bool:
        if not filter_text:
            return True
        path_text = " ".join(self._format_path_component(part) for part in path).lower()
        if filter_text in path_text:
            return True
        if isinstance(value, str):
            return filter_text in value.lower()
        if isinstance(value, bool):
            return filter_text in str(value).lower()
        if isinstance(value, numbers.Number):
            return filter_text in str(value).lower()
        if isinstance(value, SEQUENCE_TYPES):
            try:
                yaml_text = dump_yaml_to_string(value)
            except Exception:
                return False
            return filter_text in yaml_text.lower()
        return False

    def _format_path_component(self, component: PathElement) -> str:
        if isinstance(component, int):
            return f"[{component}]"
        return str(component)

    def _select_path(self, path: tuple[PathElement, ...]) -> None:
        item_id = self.path_index.get(path)
        if item_id:
            self.tree.selection_set(item_id)
            self.tree.see(item_id)

    def _on_tree_select(self, event: tk.Event) -> None:
        selection = self.tree.selection()
        if not selection:
            self.selected_node = None
            self._show_detail_frame(self.no_selection_frame)
            self.path_var.set("Select a parameter from the tree.")
            self.type_var.set("—")
            self.yaml_button.state(["disabled"])
            self.copy_path_button.state(["disabled"])
            return
        item_id = selection[0]
        node = self.node_index.get(item_id)
        if node is None:
            return
        self.selected_node = node
        self.path_var.set(self._format_path(node.path))
        self.type_var.set(self._describe_type(node.value))
        self.yaml_button.state(["!disabled"])
        self.copy_path_button.state(["!disabled"])

        if isinstance(node.value, bool):
            self.bool_var.set(bool(node.value))
            self._show_detail_frame(self.bool_frame)
        elif isinstance(node.value, numbers.Number) and not isinstance(node.value, bool):
            self.scalar_var.set(str(node.value))
            self._show_detail_frame(self.scalar_frame)
        elif isinstance(node.value, str) or node.value is None or isinstance(node.value, ScalarString):
            display = "" if node.value is None else str(node.value)
            self.scalar_var.set(display)
            self._show_detail_frame(self.scalar_frame)
        elif isinstance(node.value, MAPPING_TYPES) or isinstance(node.value, SEQUENCE_TYPES):
            self._show_detail_frame(self.complex_frame)
        else:
            display = "" if node.value is None else str(node.value)
            self.scalar_var.set(display)
            self._show_detail_frame(self.scalar_frame)

    def _format_path(self, path: tuple[PathElement, ...]) -> str:
        if not path:
            return "<root>"
        parts = [self._format_path_component(part) for part in path]
        return " → ".join(parts)

    def _show_detail_frame(self, frame: ttk.Frame) -> None:
        for candidate in self.detail_frames:
            candidate.grid_remove()
        frame.grid(row=0, column=0, sticky="w")

    # ------------------------------------------------------------------
    # Editing helpers
    def apply_scalar_change(self) -> None:
        node = self.selected_node
        if node is None:
            return
        try:
            new_value = convert_text_to_value(self.scalar_var.get(), node.value)
        except ValueError as exc:
            messagebox.showwarning("Invalid value", str(exc))
            return
        self._set_node_value(node, new_value)
        self.set_status(f"Updated {self._format_path(node.path)}")

    def apply_bool_change(self) -> None:
        node = self.selected_node
        if node is None:
            return
        new_value = bool(self.bool_var.get())
        self._set_node_value(node, new_value)
        self.set_status(f"Updated {self._format_path(node.path)}")

    def open_yaml_editor(self) -> None:
        node = self.selected_node
        if node is None:
            return
        initial_text = dump_yaml_to_string(node.value).rstrip("\n")
        dialog = tk.Toplevel(self.root)
        dialog.title(f"Edit YAML – {self._format_path(node.path)}")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.geometry("720x520")

        ttk.Label(dialog, text="Adjust the YAML content below:").pack(anchor="w", padx=12, pady=(12, 4))
        text_widget = ScrolledText(dialog, wrap="none", undo=True)
        text_widget.pack(fill="both", expand=True, padx=12, pady=0)
        text_widget.insert("1.0", initial_text)
        text_widget.focus_set()

        button_frame = ttk.Frame(dialog)
        button_frame.pack(fill="x", padx=12, pady=12)

        def apply_change() -> None:
            raw_text = text_widget.get("1.0", "end-1c")
            try:
                parsed = YAML_LOADER.load(raw_text) if raw_text.strip() else None
            except Exception as exc:
                messagebox.showerror("Invalid YAML", f"Could not parse YAML: {exc}")
                return

            if isinstance(node.value, MAPPING_TYPES):
                replacement: Any
                if parsed is None:
                    replacement = CommentedMap()
                else:
                    replacement = parsed
                if not isinstance(replacement, MAPPING_TYPES):
                    messagebox.showerror("Invalid YAML", "The YAML must describe a mapping (dictionary).")
                    return
                if isinstance(node.value, CommentedMap) and isinstance(replacement, CommentedMap):
                    node.value.clear()
                    for key, value in replacement.items():
                        node.value[key] = value
                    new_value = node.value
                else:
                    new_value = replacement
            elif isinstance(node.value, SEQUENCE_TYPES):
                if parsed is None:
                    parsed = []
                if not isinstance(parsed, (list, tuple, CommentedSeq)):
                    messagebox.showerror("Invalid YAML", "The YAML must describe a list.")
                    return
                if isinstance(node.value, CommentedSeq) and isinstance(parsed, CommentedSeq):
                    node.value.clear()
                    node.value.extend(parsed)
                    new_value = node.value
                else:
                    new_value = list(parsed)
            else:
                new_value = parsed
            self._set_node_value(node, new_value)
            dialog.destroy()
            self.set_status(f"Updated {self._format_path(node.path)} using YAML editor")

        ttk.Button(button_frame, text="Cancel", command=dialog.destroy).pack(side="right")
        ttk.Button(button_frame, text="Apply", command=apply_change).pack(side="right", padx=(0, 8))

    def _set_node_value(self, node: NodeRef, new_value: Any) -> None:
        parent = node.parent
        if parent is None:
            state = self.current_state
            if state is not None:
                state.data = new_value
        elif isinstance(parent, CommentedMap) or isinstance(parent, dict):
            parent[node.key] = new_value  # type: ignore[index]
        elif isinstance(parent, CommentedSeq) or isinstance(parent, list):
            parent[node.key] = new_value  # type: ignore[index]
        else:
            raise TypeError(f"Unsupported parent type: {type(parent)!r}")

        self.rebuild_tree(select_path=node.path)
        self.update_action_states()

    def copy_path_to_clipboard(self) -> None:
        node = self.selected_node
        if node is None:
            return
        text = self._format_path(node.path)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status(f"Copied path {text} to clipboard")

    # ------------------------------------------------------------------
    # Snakemake integration
    def run_snakemake_workflow(self) -> None:
        if self.snakemake_running:
            messagebox.showinfo(
                "Snakemake running",
                "A Snakemake workflow is already running. The output window has been brought to the front.",
            )
            if self.snakemake_dialog is not None:
                self.snakemake_dialog.lift()
            return

        command_text = self.snakemake_command_var.get().strip()
        if not command_text:
            messagebox.showerror(
                "Invalid command",
                "Please provide the Snakemake command to execute.",
            )
            return

        try:
            command_args = shlex.split(command_text)
        except ValueError as exc:
            messagebox.showerror(
                "Invalid command",
                f"Could not parse the Snakemake command: {exc}",
            )
            return

        if not command_args:
            messagebox.showerror(
                "Invalid command",
                "The Snakemake command is empty after parsing.",
            )
            return

        executable = command_args[0]
        if shutil.which(executable) is None:
            messagebox.showerror(
                "Command not available",
                f"The command '{executable}' was not found in the current environment. Check your PATH and try again.",
            )
            return

        snakefile_path: Path | None = None
        for index, argument in enumerate(command_args):
            if argument in {"--snakefile", "-s"}:
                if index + 1 >= len(command_args):
                    messagebox.showerror(
                        "Invalid command",
                        "The Snakemake command is missing a value after the --snakefile/-s option.",
                    )
                    return
                candidate = Path(command_args[index + 1])
                if not candidate.is_absolute():
                    candidate = (REPO_ROOT / candidate).resolve()
                snakefile_path = candidate
                break

        if snakefile_path is None:
            snakefile_path = SNAKEMAKE_SNAKEFILE

        if not snakefile_path.exists():
            try:
                display_path = snakefile_path.relative_to(REPO_ROOT)
            except ValueError:
                display_path = snakefile_path
            messagebox.showerror(
                "Snakefile not found",
                f"Could not find {display_path}. Make sure the workflow files are available.",
            )
            return

        self.snakemake_running = True
        self.run_snakemake_button.state(["disabled"])
        self.snakemake_queue = queue.Queue()

        dialog = tk.Toplevel(self.root)
        dialog.title("Running Snakemake workflow")
        dialog.geometry("800x480")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.protocol("WM_DELETE_WINDOW", self._on_snakemake_close_requested)
        self.snakemake_dialog = dialog

        ttk.Label(dialog, text=f"Executing: {command_text}", wraplength=760).pack(
            anchor="w", padx=12, pady=(12, 4)
        )
        text_widget = ScrolledText(dialog, wrap="none", state="disabled")
        text_widget.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.snakemake_text = text_widget

        button_frame = ttk.Frame(dialog)
        button_frame.pack(fill="x", padx=12, pady=(0, 12))
        self.snakemake_close_button = ttk.Button(
            button_frame,
            text="Close",
            command=self._close_snakemake_dialog,
            state="disabled",
        )
        self.snakemake_close_button.pack(side="right")

        command_args = list(command_args)

        def worker() -> None:
            try:
                process = subprocess.Popen(
                    command_args,
                    cwd=str(REPO_ROOT),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                )
            except FileNotFoundError:
                if self.snakemake_queue is not None:
                    self.snakemake_queue.put(("output", f"Command '{executable}' not found.\\n"))
                    self.snakemake_queue.put(("exit", 127))
                return

            if process.stdout is None:
                if self.snakemake_queue is not None:
                    self.snakemake_queue.put(("output", "Failed to capture Snakemake output.\n"))
                    self.snakemake_queue.put(("exit", process.wait()))
                return

            for line in process.stdout:
                if self.snakemake_queue is not None:
                    self.snakemake_queue.put(("output", line))
            return_code = process.wait()
            if self.snakemake_queue is not None:
                self.snakemake_queue.put(("exit", return_code))

        self.snakemake_thread = threading.Thread(target=worker, daemon=True)
        self.snakemake_thread.start()
        self.root.after(100, self._poll_snakemake_queue)
        self.set_status("Running Snakemake workflow…")

    def _poll_snakemake_queue(self) -> None:

        queue_obj = self.snakemake_queue
        if queue_obj is None:
            return
        try:
            while True:
                kind, payload = queue_obj.get_nowait()
                if kind == "output":
                    self._append_snakemake_output(str(payload))
                elif kind == "exit":
                    self._finish_snakemake(int(payload))
                    break
        except queue.Empty:
            pass

        if self.snakemake_running:
            self.root.after(100, self._poll_snakemake_queue)

    def _append_snakemake_output(self, text: str) -> None:
        widget = self.snakemake_text
        if widget is None:
            return
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.see("end")
        widget.configure(state="disabled")

    def _finish_snakemake(self, exit_code: int) -> None:
        if not self.snakemake_running:
            return

        self.snakemake_running = False
        self.run_snakemake_button.state(["!disabled"])
        if self.snakemake_close_button is not None:
            self.snakemake_close_button.state(["!disabled"])

        self._append_snakemake_output(f"\nProcess completed with exit code {exit_code}.\n")
        if exit_code == 0:
            self.set_status("Snakemake workflow finished successfully.")
        else:
            self.set_status(f"Snakemake workflow failed (exit code {exit_code}).")
            messagebox.showerror(
                "Snakemake failed",
                f"The Snakemake workflow exited with code {exit_code}. Review the log output for details.",
            )

        self.snakemake_thread = None
        self.snakemake_queue = None

    def _on_snakemake_close_requested(self) -> None:
        if self.snakemake_running:
            messagebox.showinfo(
                "Snakemake running",
                "Wait for the Snakemake workflow to finish before closing this window.",
            )
            return
        self._close_snakemake_dialog()

    def _close_snakemake_dialog(self) -> None:
        if self.snakemake_dialog is not None:
            self.snakemake_dialog.destroy()
        self.snakemake_dialog = None
        self.snakemake_text = None
        self.snakemake_close_button = None

    # ------------------------------------------------------------------
    # Diff and status helpers
    def show_diff(self) -> None:
        state = self.current_state
        if state is None:
            return
        current_text = dump_yaml_to_string(state.data).splitlines()
        saved_text = dump_yaml_to_string(state.saved).splitlines()
        diff = list(
            difflib.unified_diff(saved_text, current_text, fromfile="saved", tofile="current", lineterm="")
        )
        dialog = tk.Toplevel(self.root)
        dialog.title("YAML diff")
        dialog.geometry("760x540")
        dialog.transient(self.root)
        dialog.grab_set()

        if not diff:
            ttk.Label(dialog, text="No differences detected.").pack(padx=12, pady=12)
            ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(0, 12))
            return

        text_widget = ScrolledText(dialog, wrap="none")
        text_widget.pack(fill="both", expand=True, padx=12, pady=12)
        text_widget.insert("1.0", "\n".join(diff))
        text_widget.configure(state="disabled")
        ttk.Button(dialog, text="Close", command=dialog.destroy).pack(pady=(0, 12))

    def set_status(self, message: str) -> None:
        self.status_var.set(message)
        self.update_action_states()

    def update_action_states(self) -> None:
        dirty = self.has_unsaved_changes()
        for button in (self.save_button, self.revert_button, self.diff_button):
            if dirty:
                button.state(["!disabled"])
            else:
                button.state(["disabled"])
        if dirty:
            self.unsaved_var.set("Unsaved changes pending. Remember to save.")
        else:
            self.unsaved_var.set("")
        self.update_title()

    def update_title(self) -> None:
        base = "LAVA China configuration editor"
        if self.current_label:
            base = f"{base} — {self.current_label}"
        if self.has_unsaved_changes():
            base = f"*{base}"
        self.root.title(base)


def main() -> None:
    root = tk.Tk()
    app = ConfigEditorApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
