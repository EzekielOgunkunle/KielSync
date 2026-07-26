"""Root URLconf for the test project.

Mounts KielSync's webhook receiver the way a host project is expected to,
so the view tests exercise real reverse() lookups and the real path
rather than a URL invented for the tests.
"""

from django.urls import include, path

urlpatterns = [
    path("kielsync/", include("kielsync.django.urls")),
]
