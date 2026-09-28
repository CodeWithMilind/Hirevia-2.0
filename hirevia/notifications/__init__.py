"""Notifications module for Hirevia"""

from hirevia.notifications.base import Notifier
from hirevia.notifications.telegram import TelegramNotifier

__all__ = ["Notifier", "TelegramNotifier"]