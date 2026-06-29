"""Ensure the Celery app is loaded when Django starts so @shared_task tasks register."""

from config.celery import app as celery_app

__all__ = ("celery_app",)
