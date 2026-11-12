"""Bedrock crossplay: Geyser/Floodgate provisioning, playit NetherNet tunnel.

This package is deliberately self-contained. It touches upstream code only
through narrow seams (the process registry, server storage, the playit secret
file) so it can be updated or removed without rewriting Fabricator's server
management.
"""
