"""Pytest configuration for Edge AI tests.

Sets up sys.path so tests can import from edge_ai/src/.
"""
import os
import sys

# Add edge_ai/ directory to sys.path so 'src' package is importable
_edge_ai_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _edge_ai_dir not in sys.path:
    sys.path.insert(0, _edge_ai_dir)

# Also add edge_ai/src/ directly so modules can be imported without 'src.' prefix
_src_dir = os.path.join(_edge_ai_dir, 'src')
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)
