"""The only package that talks to the n8n instance.

Holds the server-owned credentials (ADR-006) and performs preflight and dispatch.
Nothing outside this package makes a network call to n8n.
"""
