from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class ObservableSet(set):
    """A set that logs removals and can invoke a callback on additions.

    Parameters
    ----------
    - logger: a logging.Logger-like object used for info messages.
    - callback: optional callable invoked with the added element.

    """

    def __init__(
        self,
        *args: object,
        logger: logging.Logger | None = None,
        callback: Callable[[str], None] | None = None,
        **kwargs: object,
    ) -> None:
        """Initialize the ObservableSet with an optional logger and callback.

        Parameters
        ----------
        logger : logging.Logger | None
            A logging.Logger instance used for info messages. If None, a default logger is created.
        callback : Callable[[str], None] | None
            Optional callable invoked when new elements are added.
        *args : object
            Arguments passed to the base set initializer.
        **kwargs : object
            Keyword arguments passed to the base set initializer.

        """
        super().__init__(*args, **kwargs)
        self._logger = logger if logger is not None else logging.getLogger(__name__)
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

    def discard(self, element: str) -> None:
        """Remove an element from the set and log the action."""
        if element in self:
            self._logger.info('Removed "%s" from ObservableSet.', element)
        else:
            self._logger.info(
                '"%s" has already been removed from ObservableSet.', element
            )
        return super().discard(element)

    def clear(self) -> None:
        """Clear all elements from the set and log the action."""
        self._logger.info("Cleared %d element(s) from ObservableSet.", len(self))
        return super().clear()
