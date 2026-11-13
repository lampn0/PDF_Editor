"""Undo/redo history of document edits.

Snapshot based: before every command the whole document is serialised to
bytes (unencrypted, uncompressed) and its encryption policy is recorded, so
``set_encryption`` run through :meth:`History.execute` is undoable like any
other edit. Undo and redo reload snapshots into the
*same* :class:`PdfDocument` object, so references to it held elsewhere (UI
widgets) stay valid. PyMuPDF ``Page`` objects obtained from ``doc.raw`` are
**invalid** after any undo/redo or failed command; re-fetch them when a
:attr:`History.changed` listener fires. No Qt here; listeners are plain
callables.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from pdf_editor.core.document import EncryptionPolicy, PdfDocument
from pdf_editor.core.errors import PdfError

_log = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 30
DEFAULT_MAX_BYTES = 300 * 1024 * 1024


@dataclass(frozen=True)
class Command:
    """A labelled edit. ``apply`` mutates the document and may return a value.

    ``apply`` must call ``doc.mark_dirty()`` when it changes the document (all
    ``pages`` functions do); a command that leaves the doc clean is treated as
    a no-op and not recorded.
    """

    label: str
    apply: Callable[[PdfDocument], object]


@dataclass
class _Entry:
    label: str
    data: bytes
    encryption: EncryptionPolicy | None
    state_id: int  # identity of the document state stored in ``data``/``encryption``


@dataclass(eq=False)
class History:
    """Undo/redo stacks for one document.

    ``max_steps`` bounds the number of undo steps; ``max_bytes`` bounds the total
    size of all stored snapshots (undo and redo). Oldest undo steps are evicted
    first, then the farthest redo steps.
    """

    doc: PdfDocument
    max_steps: int = DEFAULT_MAX_STEPS
    max_bytes: int = DEFAULT_MAX_BYTES
    changed: list[Callable[[], None]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.max_bytes < 1:
            raise ValueError("max_bytes must be >= 1")
        self._undo: list[_Entry] = []
        self._redo: list[_Entry] = []  # last element = next redo
        self._next_id = 1
        self._current_id = 0
        self._saved_id: int | None = None if self.doc.is_dirty else 0
        self._saved_encryption = self.doc.encryption
        self.last_execute_undoable = True
        """False when the last recorded command cannot be undone (its snapshot
        alone exceeded ``max_bytes``); the UI can warn the user."""

    # -- queries ------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def undo_label(self) -> str | None:
        return self._undo[-1].label if self._undo else None

    @property
    def redo_label(self) -> str | None:
        return self._redo[-1].label if self._redo else None

    @property
    def is_clean(self) -> bool:
        """True when the document matches the last saved (or opened) state.

        The saved state is the state id plus the encryption policy; the policy is
        compared too so a change made outside ``execute`` is never reported clean.
        """
        return self._current_id == self._saved_id and self.doc.encryption == self._saved_encryption

    @property
    def undo_count(self) -> int:
        return len(self._undo)

    @property
    def redo_count(self) -> int:
        return len(self._redo)

    @property
    def stored_bytes(self) -> int:
        return sum(len(e.data) for e in self._undo) + sum(len(e.data) for e in self._redo)

    # -- operations ---------------------------------------------------------

    def execute(self, cmd: Command) -> object:
        """Snapshot, run ``cmd`` and record it. On failure restore and re-raise.

        A command that does not mark the document dirty is a no-op: nothing is
        recorded and the dirty flag is left as it was.

        If restoring after a failure also fails, the document is left partially
        modified: the pre-command snapshot is recorded as an undo step labelled
        ``"<label> (failed)"``, the document is marked dirty, listeners are
        notified, and the original exception is re-raised with a note.
        """
        before = self.doc.to_bytes()
        before_encryption = self.doc.encryption
        was_dirty = self.doc.is_dirty
        self.doc.mark_clean()  # lets us detect whether apply changed anything
        try:
            result = cmd.apply(self.doc)
        except BaseException as exc:
            restored = True
            try:
                self.doc.restore(before, before_encryption)
            except Exception as restore_exc:
                exc.add_note(f"History rollback failed: {restore_exc!r}")
                restored = False
            if restored:
                self._set_dirty(was_dirty)
            else:
                self._record(f"{cmd.label} (failed)", before, before_encryption)
                self.doc.mark_dirty()
                self._notify()
            raise  # always the original exception
        if not self.doc.is_dirty:
            self._set_dirty(was_dirty)
            return result
        self._record(cmd.label, before, before_encryption)
        self._sync_dirty()
        self._notify()
        return result

    def _record(self, label: str, before: bytes, encryption: EncryptionPolicy | None) -> None:
        """Push the pre-command snapshot and move to a new state id."""
        previous_id = self._current_id
        self._undo.append(_Entry(label, before, encryption, previous_id))
        self._redo.clear()
        self._current_id = self._new_id()
        self._evict()
        self.last_execute_undoable = bool(self._undo) and self._undo[-1].state_id == previous_id
        if not self.last_execute_undoable:
            _log.warning(
                "Step %r cannot be undone: snapshot of %d bytes exceeds max_bytes=%d",
                label,
                len(before),
                self.max_bytes,
            )

    def undo(self) -> None:
        if not self._undo:
            raise PdfError("Nothing to undo")
        entry = self._undo[-1]
        current = self.doc.to_bytes()
        current_encryption = self.doc.encryption
        self.doc.restore(entry.data, entry.encryption)
        self._undo.pop()
        self._redo.append(_Entry(entry.label, current, current_encryption, self._current_id))
        self._current_id = entry.state_id
        self._evict()
        self._sync_dirty()
        self._notify()

    def redo(self) -> None:
        if not self._redo:
            raise PdfError("Nothing to redo")
        entry = self._redo[-1]
        current = self.doc.to_bytes()
        current_encryption = self.doc.encryption
        self.doc.restore(entry.data, entry.encryption)
        self._redo.pop()
        self._undo.append(_Entry(entry.label, current, current_encryption, self._current_id))
        self._current_id = entry.state_id
        self._evict()
        self._sync_dirty()
        self._notify()

    def save(self) -> None:
        """Save the document to its path and record the saved point."""
        self.doc.save()
        self.mark_saved()

    def save_as(self, path: str | os.PathLike[str]) -> None:
        """Save the document to ``path`` and record the saved point."""
        self.doc.save_as(path)
        self.mark_saved()

    def mark_saved(self) -> None:
        """Record the current state as the saved one (prefer :meth:`save`)."""
        self._saved_id = self._current_id
        self._saved_encryption = self.doc.encryption
        self._sync_dirty()
        self._notify()

    def clear(self) -> None:
        """Drop all undo/redo steps. The clean/dirty status is kept."""
        self._undo.clear()
        self._redo.clear()
        self._notify()

    # -- internals ----------------------------------------------------------

    def _new_id(self) -> int:
        value = self._next_id
        self._next_id += 1
        return value

    def _evict(self) -> None:
        # pop(0) is O(n) but the stacks hold at most a few dozen entries.
        while len(self._undo) > self.max_steps:
            self._undo.pop(0)
        total = self.stored_bytes
        while total > self.max_bytes and self._undo:
            total -= len(self._undo.pop(0).data)
        while total > self.max_bytes and self._redo:
            total -= len(self._redo.pop(0).data)

    def _set_dirty(self, dirty: bool) -> None:
        if dirty:
            self.doc.mark_dirty()
        else:
            self.doc.mark_clean()

    def _sync_dirty(self) -> None:
        self._set_dirty(not self.is_clean)

    def _notify(self) -> None:
        for listener in list(self.changed):
            try:
                listener()
            except Exception:
                _log.exception("History listener %r failed", listener)
