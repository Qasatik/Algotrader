"""Multi-tenant SaaS layer for the carry-bot.

Turns the single-admin bot into a subscription service where each user connects
their own Bybit API key (BYOK) and the platform runs an isolated trading
instance per user.
"""
