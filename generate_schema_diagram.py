#!/usr/bin/env python3
"""Generate database schema visualization from SQLAlchemy models.

This script uses tools.model_visualizer to create an entity-relationship
diagram from the SQLAlchemy models. The visualization is always up-to-date with
the actual database schema defined in the models.

Usage:
    uv run python generate_schema_diagram.py

Output:
    docs/schema_diagram.svg - Entity-relationship diagram showing all tables,
                              columns, relationships, and constraints
"""

import os
import sys

# Add the project root to the path to import app modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from tools.model_visualizer import generate_data_model_diagram
except ModuleNotFoundError as exc:
    if exc.name != "graphviz":
        raise
    print(
        "Missing dev dependency 'graphviz'. "
        "Run this script with: uv run python generate_schema_diagram.py",
        file=sys.stderr,
    )
    raise SystemExit(1) from exc

# Import the models package to register every mapped model with Base.
from app.db import Base, models  # noqa: F401


def main():
    """Generate the schema diagram."""
    # Ensure docs directory exists
    os.makedirs("docs", exist_ok=True)

    output_file = "docs/schema_diagram"  # Extension will be added automatically

    print("Generating database schema diagram...")

    models = sorted(
        (mapper.class_ for mapper in Base.registry.mappers), key=lambda model: model.__tablename__
    )

    print(f"Models found: {len(models)} tables")
    for model in models:
        print(f"  - {model.__tablename__}")

    # Generate the diagram
    generate_data_model_diagram(models, output_file=output_file)

    print(f"\n✓ Schema diagram generated: {output_file}.svg")
    print("\nThe diagram shows:")
    print("  • All tables with their columns and data types")
    print("  • Models grouped by their Python module")
    print("  • Primary keys (PK) and foreign keys (FK)")
    print("  • Relationships between tables")
    print("  • Unique constraints")
    print("\nTo regenerate this diagram after model changes:")
    print("  uv run python generate_schema_diagram.py")


if __name__ == "__main__":
    main()
