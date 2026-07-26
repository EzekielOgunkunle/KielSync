"""URL configuration for KielSync's webhook receiver.

Include it from the project's root URLconf:

.. code-block:: python

    urlpatterns = [
        path("kielsync/", include("kielsync.django.urls")),
    ]

which mounts the receiver at ``/kielsync/webhooks/<gateway>/``.

One URL per gateway rather than a single shared endpoint with the gateway
inferred from the body: the adapter has to be chosen *before* the payload
can be authenticated, and choosing it from unauthenticated content would
let a caller pick which signature scheme it is checked against.
"""

from django.urls import path

from kielsync.django import views

app_name = "kielsync"

urlpatterns = [
    path("webhooks/<str:gateway>/", views.webhook, name="webhook"),
]
