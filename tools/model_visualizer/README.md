# Model visualizer

Adapted from the rendering function in
[sqlalchemy-data-model-visualizer 0.1.3](https://pypi.org/project/sqlalchemy-data-model-visualizer/0.1.3/)
by Jeff Emanuel. The original MIT copyright and permission notice are preserved
verbatim in [LICENSE](LICENSE).

Local changes:

- Group model nodes by their defining Python module using Graphviz clusters.
- Give each module group a distinct muted background colour.
- Draw relationships between modules outside the clusters.
- Use qualified node IDs so identically named models from different modules stay distinct.
- Sort models for consistent output and escape HTML table labels.
- Return the generated graph and disable automatic preview by default.
- Omit the unused demonstration models, SVG interactivity helper, and fabricated documentation links.

The tool uses the project's SQLAlchemy dependency and the `graphviz` Python dev
dependency. Graphviz's system `dot` executable must also be installed; see the
repository README for installation instructions. No `lxml` dependency is needed.

Regenerate the complete schema from the repository root:

```sh
uv run python generate_schema_diagram.py
```

`generate_data_model_diagram()` accepts `group_by_module=False` for an ungrouped
diagram and `view=True` to open the rendered SVG automatically.
