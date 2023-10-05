from __future__ import annotations

import itertools

from intake.readers import datatypes
from intake.readers.entry import Catalog, DataDescription, ReaderDescription
from intake.readers.readers import BaseReader
from intake.readers.utils import LazyDict


class TiledLazyEntries(LazyDict):
    def __init__(self, client):
        self.client = client

    def __getitem__(self, item: str) -> ReaderDescription:
        from intake.readers.readers import TiledClient

        client = self.client[item]
        data = datatypes.TiledService(url=client.uri, metadata=client.item)
        if type(client).__name__ == "Node":
            reader = TiledCatalogReader(data=data)
        else:
            reader = TiledClient(data, output_instance=f"{type(client).__module__}:{type(client).__name__}")
        return reader.to_entry()

    def __len__(self):
        return len(self.client)

    def __iter__(self):
        return iter(self.client)

    def __repr__(self):
        return f"TiledEntries {sorted(set(self))}"


class TiledCatalogReader(BaseReader):
    """Creates a catalog of Tiled datasets from a root URL

    The generated catalog is lazy, only the list of entries is got eagerly, but they are
    fetched only on demand.
    """

    implements = {datatypes.TiledService}
    output_instance = "intake.readers.entry:Catalog"
    imports = {"tiled"}

    def _read(self, data, **kwargs):
        from tiled.client import from_uri

        opts = data.options.copy()
        opts.update(kwargs)
        client = from_uri(data.url, **opts)
        entries = TiledLazyEntries(client)
        return Catalog(entries=entries, aliases={k: k for k in sorted(client)}, metadata=client.item)


class SQLAlchemyCatalog(BaseReader):
    """Uses SQLAlchemy to get the list of tables at some SQL URL

    These tables are presented as data entries in a catalog, but could then be loaded by
    any reader that implements SQLQuery.
    """

    implements = {datatypes.Service}
    imports = {"sqlalchemy"}
    output_instance = "intake.readers.entry:Catalog"

    def _read(self, data, views=True, schema=None, **kwargs):
        import sqlalchemy

        engine = sqlalchemy.create_engine(data.url)
        meta = sqlalchemy.MetaData()
        meta.reflect(bind=engine, views=views, schema=schema)
        tables = list(meta.tables)
        entries = {name: DataDescription("intake.readers.datatypes:SQLQuery", kwargs={"conn": data.url, "query": name}) for name in tables}
        return Catalog(data=entries)


class StacCatalogReader(BaseReader):
    # STAC organisation: Catalog->Item->Asset. Catalogs can reference Catalogs.
    # also have ItemCollection (from searching a Catalog) and CombinedAsset (multi-layer data)
    # Asset and CombinedAsset are datasets, the rest are Catalogs

    implements = {datatypes.STACJSON, datatypes.Literal}
    imports = {"pystac"}
    output_instance = "intake.readers.entry:Catalog"

    def _read(self, data, cls: str = "Catalog", **kwargs):
        import pystac

        cls = getattr(pystac, cls)
        if isinstance(data, datatypes.JSONFile):
            self._stac = cls.from_file(data.url)
        else:
            self._stac = cls.from_dict(data.data)
        metadata = self._stac.to_dict()
        metadata.pop("links")
        self.metadata.update(metadata)
        import pystac

        cat = Catalog(metadata=self.metadata)
        items = []
        if isinstance(self._stac, pystac.Catalog):
            items = itertools.chain(self._stac.get_children(), self._stac.get_items())
        elif isinstance(self._stac, pystac.ItemCollection):
            items = self._stac.items
        elif isinstance(self._stac, pystac.Item):
            # these make assets, which are not catalogs
            for key, value in self._stac.assets.items():
                cat[key] = self._get_reader(value).to_entry()

        for subcatalog in items:
            subcls = type(subcatalog).__name__

            # TODO: items may also be readable by stack_bands
            cat[subcatalog.id] = ReaderDescription(
                reader=self.qname(),
                kwargs={"cls": subcls, "data": datatypes.Literal(subcatalog.to_dict())},
            )
        return cat

    @staticmethod
    def _get_reader(asset):
        """
        Assign intake driver for data I/O
        """
        url = asset.href
        mime = asset.media_type

        if mime in ["", "null"]:
            mime = None

        # if mimetype not registered try rasterio driver
        storage_options = asset.extra_fields.get("xarray:storage_options", {})
        cls = datatypes.recommend(url, mime=mime, storage_options=storage_options)
        meta = asset.to_dict()
        if cls:
            data = cls[0](url=url, metadata=meta, storage_options=storage_options)
        else:
            raise ValueError
        try:
            return data.to_reader(outtype="xarray:DataSet")
        except ValueError:
            # no xarray reader
            if data.possible_readers:
                return data.to_reader()
            else:
                return data


class StackBands(BaseReader):
    implements = {datatypes.STACJSON, datatypes.Literal}
    imports = {"pystac", "xarray"}
    output_instance = "intake.readers.readers:XarrayReader"

    def _read(self, data, bands, concat_dim="band"):
        # this should be a separate reader for STACJSON,
        import pystac
        from pystac.extensions.eo import EOExtension

        cls = pystac.Item
        if isinstance(data, datatypes.JSONFile):
            self._stac = cls.from_file(data.url)
        else:
            self._stac = cls.from_dict(data.data)

        band_info = [band.to_dict() for band in EOExtension.ext(self._stac).bands]
        metadatas = {}
        titles = []
        hrefs = []
        types = []
        assets = self._stac.assets
        for band in bands:
            # band can be band id, name or common_name
            if band in assets:
                info = next(
                    (b for b in band_info if b.get("id", b.get("name")) == band),
                    None,
                )
            else:
                info = next((b for b in band_info if b.get("common_name") == band), None)
                if info is not None:
                    band = info.get("id", info.get("name"))

            if band not in assets or info is None:
                valid_band_names = []
                for b in band_info:
                    valid_band_names.append(b.get("id", b.get("name")))
                    valid_band_names.append(b.get("common_name"))
                raise ValueError(f"{band} not found in list of eo:bands in collection." f"Valid values: {sorted(list(set(valid_band_names)))}")
            asset = assets.get(band)
            metadatas[band] = asset.to_dict()
            titles.append(band)
            types.append(asset.media_type)
            hrefs.append(asset.href)

        unique_types = set(types)
        if len(unique_types) != 1:
            raise ValueError(f"Stacking failed: bands must have same type, multiple found: {unique_types}")
        reader = StacCatalogReader._get_reader(asset)
        reader.kwargs["concat_dim"] = concat_dim
        reader.kwargs["data"].url = hrefs
        return reader


class StacSearch(BaseReader):
    implements = {datatypes.STACJSON}
    imports = {"pystac"}
    output_instance = "intake.readers.entry:Catalog"

    def __init__(self, query, metadata=None, **kwargs):
        self.query = query
        super().__init__(metadata=metadata, **kwargs)

    def _read(self, data, query=None, **kwargs):
        import requests
        from pystac import ItemCollection

        query = query or self.query
        req = requests.post(data.url + "/search", json=query)
        out = req.json()
        cat = Catalog(metadata=self.metadata)
        items = ItemCollection.from_dict(out).items
        for subcatalog in items:
            subcls = type(subcatalog).__name__
            cat[subcatalog.id] = ReaderDescription(
                data=datatypes.Literal(subcatalog.to_dict()).to_entry(),
                reader=StacCatalogReader.qname(),
                kwargs={"cls": subcls},
            )
        return cat


class THREDDSCatalog(Catalog):
    ...


class THREDDSCatalogReader(BaseReader):
    implements = {datatypes.THREDDSCatalog}
    output_instance = THREDDSCatalog.qname()
    imports = {"siphon", "xarray"}

    def _read(self, data, **kwargs):
        from siphon.catalog import TDSCatalog

        from intake.readers.readers import XArrayDatasetReader

        thr = TDSCatalog(data.url)
        cat = Catalog(metadata=thr.metadata)
        for r in thr.catalog_refs.values():
            cat[r.title] = THREDDSCatalogReader(datatypes.THREDDSCatalog(url=r.href))
        for ds in thr.datasets.values():
            cat[ds.id + "_DAP"] = XArrayDatasetReader(datatypes.Service(ds.access_urls["OpenDAP"]), engine="pydap")
            cat[ds.id + "_CDF"] = XArrayDatasetReader(datatypes.HDF5(ds.access_urls["HTTPServer"]), engine="h5netcdf")

        return cat


class HuggingfaceHubCatalog(BaseReader):
    output_instance = "intake.readers.entry:Catalog"
    imports = {"datasets"}

    def _read(self, *args, with_community_datasets=False, **kwargs):
        from intake.readers.datatypes import HuggingfaceDataset
        from intake.readers.readers import HuggingfaceReader
        import huggingface_hub

        datasets = huggingface_hub.list_datasets(full=False)
        if not with_community_datasets:
            datasets = [dataset for dataset in datasets if "/" not in dataset.id]
        entries = [HuggingfaceReader(data=HuggingfaceDataset(d.id, metadata=d.__dict__)) for d in datasets]
        cat = Catalog(entries=entries)
        cat.aliases = {d.id: e for d, e in zip(datasets, cat.entries)}
        return cat


class SKLearnExamplesCatalog(BaseReader):
    output_instance = "intake.readers.entry:Catalog"
    imports = {"sklearn"}

    def _read(self, **kw):
        from intake.readers.readers import SKLearnExampleReader
        import sklearn.datasets

        names = [funcname[5:] for funcname in dir(sklearn.datasets) if funcname.startswith("load_")]
        entries = [SKLearnExampleReader(name=name) for name in names]
        cat = Catalog(entries=entries)
        cat.aliases = {name: e for name, e in zip(names, cat.entries)}
        return cat


class TorchDatasetsCatalog(BaseReader):
    output_instance = "intake.readers.entry:Catalog"
    imports = {"datasets"}

    def _read(self, rootdir, *args, **kwargs):
        from intake.readers.readers import TorchDataset
        import importlib

        cat = Catalog()
        for name in ("vision", "audio", "text"):
            try:
                mod = importlib.import_module(f"torch{name}")
                for func in mod.datasets.__all__:
                    f = getattr(mod.datasets, func)
                    metadata = {"description": f.__doc__.split("\n", 1)[0]} if f.__doc__ else None
                    tok = cat.add_entry(TorchDataset(name, func, rootdir, metadata=metadata))
                    cat.aliases[func] = tok
            except (ImportError, ModuleNotFoundError):
                pass
        return cat
