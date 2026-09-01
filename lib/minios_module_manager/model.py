"""Small UI-independent state records for MiniOS Module Manager."""


class LoadState(object):
    LOADING = 'loading'
    READY = 'ready'
    EMPTY = 'empty'
    UNAVAILABLE = 'unavailable'
    ERROR = 'error'

    VALUES = (LOADING, READY, EMPTY, UNAVAILABLE, ERROR)


class ModuleRecord(object):
    """One module entry returned by an authoritative backend."""

    def __init__(self, name, mount=None, source=None, origin=None,
                 removable=False):
        self.name = name
        self.mount = mount
        self.source = source
        self.origin = origin
        self.removable = bool(removable)


class InspectionEntry(object):
    """One rootless filesystem entry inside an immutable module."""

    def __init__(self, path, kind='unknown', size=None, mode=None, target=None):
        self.path = path
        self.kind = kind
        self.size = size
        self.mode = mode
        self.target = target


class Inspection(object):
    """Rootless inspection result for one immutable module."""

    def __init__(self, state=LoadState.LOADING, path=None, size=None,
                 entries=None, message=''):
        if state not in LoadState.VALUES:
            raise ValueError('invalid inspection state')
        self.state = state
        self.path = path
        self.size = size
        self.entries = tuple(entries or ())
        self.message = message


class Snapshot(object):
    """One authoritative module snapshot and its presentation state."""

    def __init__(self, state=LoadState.LOADING, modules=None, message='',
                 union_backend=None, data_root=None, bundle_extension=None,
                 add_available=False):
        if state not in LoadState.VALUES:
            raise ValueError('invalid snapshot state')
        self.state = state
        self.modules = tuple(modules or ())
        self.message = message
        self.union_backend = union_backend
        self.data_root = data_root
        self.bundle_extension = bundle_extension
        self.add_available = bool(add_available)

    @property
    def usable(self):
        return self.state in (LoadState.READY, LoadState.EMPTY)
