"""
Layer 3: CONTEXT MANAGER
Single source of truth for active context.
Applies priority rules and expiry.
Emits context_updated signal when context changes.
"""
import time
import threading
from PyQt6.QtCore import QObject, pyqtSignal
from context_extractor import Context


# Expiry times in seconds
EXPIRY = {
    'selection': 5.0,
    'region':    30.0,
    'ocr':       10.0,
    'window':    20.0,
}


class ContextManager(QObject):
    context_updated = pyqtSignal(object)  # emits Context object

    def __init__(self):
        super().__init__()
        self._lock           = threading.Lock()
        self._active         = Context()
        self._window_ctx     = Context()
        self._selection_ctx  = Context()
        self._region_ctx     = Context()
        self._expiry_thread  = threading.Thread(
            target=self._expiry_loop, daemon=True
        )
        self._expiry_thread.start()

    def update(self, ctx: Context) -> None:
        """Receive a new context and apply priority rules."""
        with self._lock:
            if ctx.source == 'window':
                # Window change resets selection and region
                self._window_ctx    = ctx
                self._selection_ctx = Context()
                self._region_ctx    = Context()
                print(f"ContextManager: Window → {ctx.window_title[:50]}")

            elif ctx.source == 'selection':
                self._selection_ctx = ctx
                print(f"ContextManager: Selection → {ctx.selected_text[:40]}")

            elif ctx.source == 'region':
                self._region_ctx = ctx
                print(f"ContextManager: Region → {ctx.visible_text[:40]}")

            self._recompute()

    def get(self) -> Context:
        with self._lock:
            return self._active

    def clear_selection(self) -> None:
        with self._lock:
            self._selection_ctx = Context()
            self._recompute()

    def clear_region(self) -> None:
        with self._lock:
            self._region_ctx = Context()
            self._recompute()

    def _recompute(self) -> None:
        """
        Priority: selected_text > region > visible_OCR > window_title
        Merge highest-priority context with window metadata.
        """
        now = time.time()

        # Check expiry
        sel_valid = (
            bool(self._selection_ctx.selected_text) and
            (now - self._selection_ctx.timestamp) < EXPIRY['selection']
        )
        reg_valid = (
            bool(self._region_ctx.visible_text or self._region_ctx.image) and
            (now - self._region_ctx.timestamp) < EXPIRY['region']
        )

        base = self._window_ctx

        if sel_valid:
            merged = Context(
                app          = base.app,
                mode         = base.mode,
                window_title = base.window_title,
                selected_text= self._selection_ctx.selected_text,
                visible_text = base.visible_text,
                url          = base.url,
                page_title   = base.page_title,
                file_path    = base.file_path,
                activity     = base.activity,
                needs        = base.needs,
                source       = 'selection',
                timestamp    = self._selection_ctx.timestamp,
            )
        elif reg_valid:
            merged = Context(
                app          = base.app,
                mode         = base.mode,
                window_title = base.window_title,
                selected_text= "",
                visible_text = self._region_ctx.visible_text,
                image        = self._region_ctx.image,
                url          = base.url,
                page_title   = base.page_title,
                activity     = base.activity,
                needs        = base.needs,
                source       = 'region',
                timestamp    = self._region_ctx.timestamp,
            )
        else:
            merged = base

        self._active = merged
        self.context_updated.emit(merged)

    def _expiry_loop(self) -> None:
        """Periodically expire stale contexts."""
        while True:
            time.sleep(1.0)
            with self._lock:
                now     = time.time()
                changed = False
                if (self._selection_ctx.selected_text and
                        (now - self._selection_ctx.timestamp) > EXPIRY['selection']):
                    self._selection_ctx = Context()
                    changed = True
                    print("ContextManager: Selection expired.")
                if (self._region_ctx.visible_text and
                        (now - self._region_ctx.timestamp) > EXPIRY['region']):
                    self._region_ctx = Context()
                    changed = True
                    print("ContextManager: Region expired.")
                if changed:
                    self._recompute()
