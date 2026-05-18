Components
==========

This section documents the GVSoC components that can be instantiated from
Python generators. Each page describes the generator class, its ports, and
the unit tests that exercise the component.

The pages are generated at doc build time by walking ``GVSOC_MODULES`` and
picking up every generator class that declares a ``__gvsoc_doc__`` class
attribute. Add such an attribute to a ``gvsoc.systree.Component`` subclass
to include it here.

A handful of components — e.g. the v2 iDMA — ship a single
hand-written page rather than the per-class auto-generated one
because they span multiple generator classes that share a back-end
pipeline. Such pages live alongside the generated tree (e.g.
``components/ips/pulp/idma_v2.rst``) and the auto-generated index
references them via the ``static_page`` field on the corresponding
``__gvsoc_doc__``, so they still appear in the same group as their
auto-generated siblings.

.. toctree::
   :maxdepth: 1

   _generated/index
