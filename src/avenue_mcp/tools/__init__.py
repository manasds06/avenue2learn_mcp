"""The MCP tool surface.

Rule from docs/05: tools never talk to HTTP directly. They call client/, which
keeps them testable against a fake client with no network.
"""
