#!/bin/bash
# Generate database schema diagram from SQLAlchemy models
# This script should be run whenever database models are added, removed, or modified

set -e

echo "Generating database schema diagram..."
uv run --extra dev python generate_schema_diagram.py

echo "✓ Database schema diagram updated successfully!"
echo "  Updated file: docs/schema_diagram.svg"
