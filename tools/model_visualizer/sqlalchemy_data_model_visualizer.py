"""Model rendering adapted from sqlalchemy-data-model-visualizer 0.1.3.

Copyright (c) 2023 Jeff Emanuel. Distributed under the MIT license; see LICENSE.
Upstream: https://github.com/Dicklesworthstone/sqlalchemy_data_model_visualizer
"""

from collections.abc import Iterable
from html import escape
from itertools import groupby
from operator import attrgetter
from typing import Any

import graphviz
from sqlalchemy import inspect
from sqlalchemy.orm import Mapper

# Muted green, cyan, amber, violet, rose, and blue inspired by Tailwind's 300 palette.
MODULE_COLORS = ("#B7D9C3", "#B6DCE2", "#E6D6AD", "#D2C5E3", "#E4C6D1", "#C6D2E2")


def generate_data_model_diagram(
    models: Iterable[type],
    output_file: str = "my_data_model_diagram",
    add_labels: bool = True,
    *,
    group_by_module: bool = True,
    view: bool = False,
) -> graphviz.Digraph:
    """Render models as SVG, optionally grouping their nodes into module clusters."""
    models = sorted(models, key=lambda model: (model.__module__, model.__name__))
    dot = graphviz.Digraph(
        comment="SQLAlchemy Data Models",
        format="svg",
        graph_attr={"bgcolor": "#EEEEEE", "rankdir": "TB", "splines": "spline"},
        node_attr={"shape": "none", "fontsize": "12", "fontname": "Roboto"},
        edge_attr={"fontsize": "10", "fontname": "Roboto"},
    )

    for index, (module, module_models) in enumerate(groupby(models, key=attrgetter("__module__"))):
        with dot.subgraph(name=f"cluster_{module}" if group_by_module else None) as cluster:
            if group_by_module:
                cluster.attr(
                    label=module,
                    style="rounded",
                    bgcolor=MODULE_COLORS[index % len(MODULE_COLORS)],
                    color="#9EA7BB",
                )
            for model in module_models:
                mapper: Mapper[Any] = inspect(model)
                name = model.__name__
                label = (
                    '<TABLE BORDER="0" CELLBORDER="1" CELLSPACING="0">'
                    '<TR><TD COLSPAN="2" BGCOLOR="#3F51B5">'
                    f'<FONT COLOR="white">{escape(name)}</FONT></TD></TR>'
                )
                for column in mapper.columns:
                    constraints = []
                    if column.primary_key:
                        constraints.append("PK")
                    if column.unique:
                        constraints.append("Unique")
                    if column.index:
                        constraints.append("Index")
                    label += (
                        f'<TR><TD BGCOLOR="#BBDEFB">{escape(column.name)}</TD>'
                        f'<TD BGCOLOR="#BBDEFB">{escape(str(column.type))} '
                        f'({",".join(constraints)})</TD></TR>'
                    )
                label += "</TABLE>"
                cluster.node(f"{module}.{name}", label=f"<{label}>")

    # Keep edges outside clusters so relationships do not pull other modules' nodes inside.
    for model in models:
        source = f"{model.__module__}.{model.__name__}"
        mapper = inspect(model)
        for relationship in mapper.relationships:
            target = relationship.mapper.class_
            dot.edge(
                source,
                f"{target.__module__}.{target.__name__}",
                label=relationship.key if add_labels else None,
                tooltip=f"Relation between {model.__name__} and {target.__name__}",
                color="#1E88E5",
                style="dashed",
            )

    dot.render(output_file, view=view)
    return dot
