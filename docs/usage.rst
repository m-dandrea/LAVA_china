Usage
=====

This guide walks through the end-to-end workflow for running the Land Availability Analysis (LAVA)
tool on a new study region. The steps below mirror the order implemented in the repository
scripts and the Snakemake pipeline.

.. contents:: Table of contents
   :local:
   :depth: 2

Overview of the workflow
------------------------

1. Prepare a Python environment and clone the repository.
2. Create the study-region configuration files in ``configs/``.
3. Populate the ``Raw_Spatial_Data`` folders with the required input datasets.
4. Run :mod:`spatial_data_prep.py` to clip, harmonise, and derive helper rasters and vectors.
5. Inspect the pre-processed data (optional but recommended) with ``data_explore.ipynb``.
6. Run :mod:`Exclusion.py` for each technology to create available-land rasters.
7. Use :mod:`suitability.py` to derive cost-modified resource grades.
8. Generate energy profiles with :mod:`energy_profiles.py` once weather cut-outs are available.
9. (Optional) Automate the workflow across many regions with the Snakemake rules in
   ``snakemake/``.

Environment setup
-----------------

.. code-block:: bash

   git clone https://github.com/jome1/LAVA.git
   cd LAVA
   conda env create -f envs/requirements.yaml
   conda activate lava

The ``requirements.yaml`` file defines all Python dependencies that are used throughout the
workflow, including ``atlite`` for resource aggregation, ``openeo`` for fetching ESA WorldCover
data, and geospatial tooling used in the preprocessing scripts.

Configuration files
-------------------

Create local configuration files based on the provided templates:

.. code-block:: bash

   cp configs/config_template_china.yaml configs/config.yaml
   cp configs/onshorewind_template_china.yaml configs/onshorewind.yaml
   cp configs/solar_template_china.yaml configs/solar.yaml

Key items to review in ``configs/config.yaml`` include:

* ``study_region_name`` and ``country_code`` which define the output directory and support
  catalogue downloads.
* ``landcover_source`` (``openeo`` for ESA WorldCover or ``file`` for local rasters) together
  with ``resolution_landcover`` or ``landcover_filename``.
* Flags for each OpenStreetMap feature class, coastline buffers, and atlas layers (wind, solar).
* ``protected_areas_source`` which can use the WDPA downloader or a local file.
* Optional helper layers such as ``compute_substation_proximity``, ``compute_road_proximity``,
  ``compute_terrain_ruggedness``, and ``forest_density``.
* ``scenario`` and ``technology`` settings that control filenames and the downstream exclusion
  runs.

Technology-specific exclusion and suitability thresholds reside in ``configs/onshorewind.yaml``
and ``configs/solar.yaml``. Adjust the resource-grade definitions, minimum area filters, and
modifier weights before running exclusions and suitability calculations.

Input data expectations
-----------------------

The repository already contains the directory tree under ``Raw_Spatial_Data/``. Populate the
folders before launching preprocessing:

* ``DEM/`` – place the DEM raster referenced by ``DEM_filename``.
* ``landcover/`` – only required when ``landcover_source`` is ``file``.
* ``GOAS/`` – optional Global Oceans and Seas geopackage (``goas.gpkg``) for coastline buffers.
* ``protected_areas/`` – local WDPA extracts when ``protected_areas_source`` is ``file``.
* ``global_solar_wind_atlas/`` – cached layers downloaded by the scripts when the respective
  flags are enabled.
* ``OSM/`` – shapefiles from Geofabrik when ``OSM_source`` is ``geofabrik`` (Overpass requests are
  handled automatically).
* ``additional_exclusion_polygons/`` and ``additional_exclusion_rasters/`` – user-defined
  supplementary constraints referenced in the configuration.
* ``weather_data/`` – atlite cut-outs used later for energy profile creation. The helper
  :mod:`weather_data_prep.py` script can be adapted to request ERA5 cut-outs if needed.

Spatial preprocessing
---------------------

Run the preprocessing script after configuring the study region. It clips the raw inputs to the
study area, aligns rasters, and computes helper layers such as slope, terrain ruggedness, and
proximity rasters.

.. code-block:: bash

   python spatial_data_prep.py --region <RegionName>

When ``landcover_source`` is ``openeo`` the script will prompt for Copernicus Data Space
credentials the first time it runs. Outputs are written to ``data/<RegionName>/`` and include:

* CRS definitions (``*_global_CRS.pkl``, ``*_local_CRS.pkl``) used downstream.
* Co-registered rasters for DEM, landcover, wind, solar, terrain ruggedness, and optional layers.
* ``derived_from_DEM/`` containing slope, aspect, terrain ruggedness, and north-facing masks.
* ``OSM_Infrastructure/`` geopackages for each enabled infrastructure category.
* ``landuses_<RegionName>.json`` and ``pixel_size_<RegionName>_<CRS>.json`` describing the
  land-cover codes and raster resolution required by the exclusion routines.

Inspecting the outputs
----------------------

Use ``data_explore.ipynb`` to verify the preprocessing results. The notebook loads data from the
``data/<RegionName>/`` folder, visualises selected layers, and summarises the available land-cover
codes to support tuning of exclusion thresholds.

Land eligibility exclusions
---------------------------

Create technology-specific available-land rasters by running :mod:`Exclusion.py`. The command-line
flags mirror the configuration entries so that single technologies or scenarios can be processed
independently.

.. code-block:: bash

   python Exclusion.py --region <RegionName> --technology onshorewind --scenario ref
   python Exclusion.py --region <RegionName> --technology solar --scenario ref

The script loads the prepared rasters and vector layers, applies the filters defined in the
technology configuration, and writes ``*_available_land_*.tif`` files under
``data/<RegionName>/available_land/``. A log of each scenario run is stored in the region folder
for traceability.

Suitability and resource grades
-------------------------------

Run :mod:`suitability.py` after both solar and wind exclusions are available. The script aligns
resource layers, applies terrain and region modifiers, and exports cost multipliers together with
thresholded resource-grade rasters in ``data/<RegionName>/suitability/``.

.. code-block:: bash

   python suitability.py --region <RegionName> --scenario ref

Energy profile simulation
-------------------------

Energy profiles combine the available land, suitability grades, and weather cut-outs. Ensure that
``configs/config.yaml`` points ``weather_data_path`` to the directory that contains the prepared
atlite cut-outs. Then run:

.. code-block:: bash

   python energy_profiles.py --region <RegionName> --technology onshorewind --scenario ref --weather_year 2019
   python energy_profiles.py --region <RegionName> --technology solar --scenario ref --weather_year 2019

Outputs are written to ``data/<RegionName>/energy_profiles/`` and include resource-grade time series
as well as diagnostic plots documenting the available area shares.

Batch processing with Snakemake
-------------------------------

For large-scale studies the ``snakemake/Snakefile`` orchestrates all stages across multiple regions,
technologies, and weather years. The workflow creates ``snakemake_log`` sentinels to prevent reruns
of completed steps. Launch it (after customising the region lists at the top of the Snakefile) with:

.. code-block:: bash

   snakemake --cores 4 --resources openeo_req=1

Use ``--snakefile snakemake/Snakefile_short`` or ``Snakefile_short_short`` for alternative presets.
The ``openeo_req`` resource serialises ESA WorldCover downloads to avoid rate limits while still
parallelising the remaining steps.

