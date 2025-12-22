"""A small ObservableSet used by the bot to track download archive state.

This file is a lightly modernized copy for preview: added type hints
and small docstrings. Behavior intentionally preserved.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional


class ObservableSet(set):
    """A set that logs removals and can invoke a callback on additions.

    Parameters
    - logger: a logging.Logger-like object used for info messages.
    - callback: optional callable invoked with the added element.
    """

    def __init__(
        self,
        *args,
        logger: logging.Logger = logging.getLogger(),
        callback: Optional[Callable[[str], None]] = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._logger = logger
        self._callback = callback

    def set_callback(
        self, callback: Callable[[str], None], overwrite: bool = False
    ) -> None:
        """Set the callback invoked when new elements are added.

        If `overwrite` is False and a callback already exists, the call is ignored.
        """
        if self._callback and not overwrite:
            return
        self._callback = callback

    def _trigger_callback(self, element: str) -> None:
        if self._callback:
            self._callback(element)

    def add(self, element: str) -> None:
        if element not in self:
            super().add(element)
            self._trigger_callback(element)

    def discard(self, element: str) -> None:
        if element in self:
            self._logger.info(f'Removed "{element}" from ObservableSet.')
        else:
            self._logger.info(
                f'"{element}" has already been removed from ObservableSet.'
            )
        return super().discard(element)

    def clear(self) -> None:
        self._logger.info(f"Cleared {len(self)} element(s) from ObservableSet.")
        return super().clear()
