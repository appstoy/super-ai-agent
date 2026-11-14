from super_agent.sdk.plugins import PluginKind, PluginLoader, PluginMetadata


def test_plugin_loader_loads_compatible_plugin_and_isolates_failure() -> None:
    loader = PluginLoader()
    good_meta = PluginMetadata("demo-tool", "1.0.0", "1", PluginKind.TOOL)
    bad_meta = PluginMetadata("old-tool", "0.1.0", "0", PluginKind.TOOL)

    good = loader.load(good_meta, lambda: {"name": "demo"})
    failed = loader.load(bad_meta, lambda: {"name": "old"})
    crashed = loader.load(good_meta, lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    assert good.loaded and good.instance == {"name": "demo"}
    assert not failed.loaded and "incompatible" in failed.error
    assert not crashed.loaded and crashed.error == "boom"
    assert len(loader.results) == 3

