import os

SECRET_KEY = "test-secret-key"

DEBUG = False

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "kielsync",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("KIELSYNC_TEST_DB_NAME", "kielsync"),
        "USER": os.environ.get("KIELSYNC_TEST_DB_USER", "kielsync"),
        "PASSWORD": os.environ.get("KIELSYNC_TEST_DB_PASSWORD", "kielsync"),
        "HOST": os.environ.get("KIELSYNC_TEST_DB_HOST", "localhost"),
        "PORT": os.environ.get("KIELSYNC_TEST_DB_PORT", "5433"),
    }
}

USE_TZ = True

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
