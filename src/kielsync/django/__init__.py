"""Django integration layer for KielSync.

This package holds everything that depends on Django: models, the app
config, migrations, and the settings/factory glue that injects
configuration into the framework-independent adapters in
``kielsync.core``. The dependency runs one way only — ``kielsync.django``
imports from ``kielsync.core``, never the reverse.
"""
