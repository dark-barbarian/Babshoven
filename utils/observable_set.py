from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class ObservableSet(set):
    """A set that logs removals and can invoke a callback on additions."""

    def __init__(
        self,
        *args: object,
        callback: Callable[[str], None] | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize the ObservableSet with an optional callback.

        Parameters
        ----------
        callback : Callable[[str], None] | None
            Optional callable invoked when new elements are added.
        *args : object
            Arguments passed to the base set initializer.
        **kwargs : object
            Keyword arguments passed to the base set initializer.

        """
        super().__init__(*args, **kwargs)
        self._logger = logging.getLogger(__name__)
        self._callback = callback

    def set_callback(
        self,
        callback: Callable[[str], None],
        *,
        overwrite: bool = False,
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
        """Add an element to the set and trigger the callback if it's new."""
        if element not in self:
            super().add(element)
            self._trigger_callback(element)

    def discard(self, element: object) -> None:
        """Remove an element from the set and log the action."""
        if element in self:
            self._logger.info('Removed "%s" from ObservableSet.', element)
        else:
            self._logger.info('"%s" has already been removed from ObservableSet.', element)
        return super().discard(element)

    def clear(self) -> None:
        """Clear all elements from the set and log the action."""
        self._logger.info("Cleared %d element(s) from ObservableSet.", len(self))
        return super().clear()
