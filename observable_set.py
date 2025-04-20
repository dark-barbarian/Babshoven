class ObservableSet(set):
    def __init__(self, *args, logger, callback=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._logger = logger
        self._callback = callback
    
    def set_callback(self, callback, overwrite = False):
        if self._callback and not overwrite:
            return
        self._callback = callback

    def _trigger_callback(self, element):
        if self._callback:
            self._callback(element)

    def add(self, element):
        if element not in self:
            super().add(element)
            self._trigger_callback(element)
    
    def discard(self, element):
        if element in self:
            self._logger.info(f"Removed \"{element}\" from ObservableSet.")
        else:
            self._logger.info(f"\"{element}\" has already been removed from ObservableSet.")
        return super().discard(element)
    
    def clear(self):
        self._logger.info(f"Cleared {len(self)} element(s) from ObservableSet.")
        return super().clear()