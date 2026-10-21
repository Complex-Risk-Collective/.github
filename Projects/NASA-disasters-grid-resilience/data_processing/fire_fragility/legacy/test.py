import h5pyd

domain = "/nrel/nsrdb/v3/nsrdb_2020.h5"

with h5pyd.File(domain, mode="r") as resource:
    print(list(resource.keys())[:10])


