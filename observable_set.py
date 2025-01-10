class ObservableSet(set):
    def __init__(self, *args, callback=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._callback = callback
    
    def set_callback(self, callback, overwrite = False):
        if self._callback and not overwrite:
            return
        self._callback = callback

    def _trigger_callback(self, element):
        if self._callback:
            self._callback(element)

    def add(self, element):
        print(f'added {element}')
        if element not in self:
            super().add(element)
            self._trigger_callback(element)
    
    def discard(self, element):
        print(f"discarded {element}")
        return super().discard(element)