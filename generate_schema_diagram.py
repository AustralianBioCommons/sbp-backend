#!/usr/bin/env python3
"""Generate database schema visualization from SQLAlchemy models.

This script uses sqlalchemy_data_model_visualizer to create an entity-relationship
diagram from the SQLAlchemy models. The visualization is always up-to-date with
the actual database schema defined in the models.

Usage:
    python generate_schema_diagram.py

Output:
    docs/schema_diagram.png - Entity-relationship diagram showing all tables,
                              columns, relationships, and constraints
"""

import os
import sys

# Add the project root to the path to import app modules
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy_data_model_visualizer import generate_data_model_diagram, add_web_font_and_interactivity

from app.db import Base, engine
# Import all models to ensure they're registered with Base
from app.db.models.core import (
    AppUser,
    Workflow,
    WorkflowRun,
    S3Object,
    RunInput,
    RunOutput,
    RunMetric,
)


def main():
    """Generate the schema diagram."""
    # Ensure docs directory exists
    os.makedirs("docs", exist_ok=True)
    
    output_file = "docs/schema_diagram"  # Extension will be added automatically
    
    print("Generating database schema diagram...")
    
    # Collect all models
    models = [AppUser, Workflow, WorkflowRun, S3Object, RunInput, RunOutput, RunMetric]
    
    print(f"Models found: {len(models)} tables")
    for model in models:
        print(f"  - {model.__tablename__}")
    
    # Generate the diagram
    generate_data_model_diagram(
        models,
        output_file=output_file
    )
    
    print(f"\n✓ Schema diagram generated: {output_file}.png")
    print("\nThe diagram shows:")
    print("  • All tables with their columns and data types")
    print("  • Primary keys (PK) and foreign keys (FK)")
    print("  • Relationships between tables")
    print("  • Unique constraints")
    print("\nTo regenerate this diagram after model changes:")
    print("  python generate_schema_diagram.py")


if __name__ == "__main__":
    main()
