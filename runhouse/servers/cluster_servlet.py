import asyncio
import copy
import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Union

import requests
from pydantic import BaseModel

import runhouse

from runhouse.constants import (
    DEFAULT_STATUS_CHECK_INTERVAL,
    INCREASED_STATUS_CHECK_INTERVAL,
    STATUS_CHECK_DELAY,
)

from runhouse.globals import configs, obj_store, rns_client
from runhouse.resources.hardware import load_cluster_config_from_file
from runhouse.rns.utils.api import ResourceAccess
from runhouse.servers.http.auth import AuthCache

from runhouse.servers.obj_store import ObjStoreError
from runhouse.utils import sync_function

logger = logging.getLogger(__name__)


class ClusterServletError(Exception):
    pass


class ResourceStatusData(BaseModel):
    cluster_config: dict
    env_resource_mapping: Dict[str, List[Dict[str, Any]]]
    system_cpu_usage: float
    system_memory_usage: Dict[str, Any]
    system_disk_usage: Dict[str, Any]
    env_servlet_processes: Dict[str, Dict[str, Any]]
    server_pid: int
    runhouse_version: str


class ClusterServlet:
    async def __init__(
        self, cluster_config: Optional[Dict[str, Any]] = None, *args, **kwargs
    ):

        # We do this here instead of at the start of the HTTP Server startup
        # because someone can be running `HTTPServer()` standalone in a test
        # and still want an initialized cluster config in the servlet.
        if not cluster_config:
            cluster_config = load_cluster_config_from_file()

        self.cluster_config: Optional[Dict[str, Any]] = (
            cluster_config if cluster_config else {}
        )
        self._initialized_env_servlet_names: Set[str] = set()
        self._key_to_env_servlet_name: Dict[Any, str] = {}
        self._auth_cache: AuthCache = AuthCache(cluster_config)

        if cluster_config.get("resource_subtype", None) == "OnDemandCluster":
            if cluster_config.get("autostop_mins") > 0:
                try:
                    from sky.skylet import configs  # noqa
                except ImportError:
                    raise ImportError(
                        "skypilot must be installed on the cluster environment to support cluster autostop. "
                        "Install using cluster.run('pip install skypilot') or adding `skypilot` to the env requirements."
                    )
            self._last_activity = time.time()
            self._last_register = None
            autostop_thread = threading.Thread(target=self.update_autostop, daemon=True)
            autostop_thread.start()

    ##############################################
    # Cluster autostop
    ##############################################
    def update_autostop(self):
        import pickle

        from sky.skylet import configs as sky_configs

        while True:
            autostop_mins = pickle.loads(
                sky_configs.get_config("autostop_config")
            ).autostop_idle_minutes
            self._last_register = float(
                sky_configs.get_config("autostop_last_active_time")
            )
            if autostop_mins > 0 and (
                not self._last_register
                or (
                    # within 2 min of autostop and there's more recent activity
                    60 * autostop_mins - (time.time() - self._last_register) < 120
                    and self._last_activity > self._last_register
                )
            ):
                sky_configs.set_config("autostop_last_active_time", self._last_activity)
                self._last_register = self._last_activity

            time.sleep(30)

    ##############################################
    # Cluster config state storage methods
    ##############################################
    async def aget_cluster_config(self) -> Dict[str, Any]:
        return self.cluster_config

    async def aset_cluster_config(self, cluster_config: Dict[str, Any]):
        self.cluster_config = cluster_config

        # Propagate the changes to all other process's obj_stores
        await asyncio.gather(
            *[
                obj_store.acall_env_servlet_method(
                    env_servlet_name,
                    "aset_cluster_config",
                    cluster_config,
                    use_env_servlet_cache=False,
                )
                for env_servlet_name in await self.aget_all_initialized_env_servlet_names()
            ]
        )

        return self.cluster_config

    async def aset_cluster_config_value(self, key: str, value: Any):
        if key == "autostop_mins" and value > -1:
            from sky.skylet import configs as sky_configs

            self._last_activity = time.time()
            sky_configs.set_config("autostop_last_active_time", self._last_activity)
        self.cluster_config[key] = value

        # Propagate the changes to all other process's obj_stores
        await asyncio.gather(
            *[
                obj_store.acall_env_servlet_method(
                    env_servlet_name,
                    "aset_cluster_config_value",
                    key,
                    value,
                    use_env_servlet_cache=False,
                )
                for env_servlet_name in await self.aget_all_initialized_env_servlet_names()
            ]
        )

        return self.cluster_config

    ##############################################
    # Auth cache internal functions
    ##############################################
    async def aresource_access_level(
        self, token: str, resource_uri: str
    ) -> Union[str, None]:
        # If the token in this request matches that of the owner of the cluster,
        # they have access to everything
        if configs.token and (
            configs.token == token
            or rns_client.cluster_token(configs.token, resource_uri) == token
        ):
            return ResourceAccess.WRITE
        return self._auth_cache.lookup_access_level(token, resource_uri)

    async def aget_username(self, token: str) -> str:
        return self._auth_cache.get_username(token)

    async def ahas_resource_access(self, token: str, resource_uri=None) -> bool:
        """Checks whether user has read or write access to a given module saved on the cluster."""
        from runhouse.rns.utils.api import ResourceAccess

        if token is None:
            # If no token is provided assume no access
            return False

        cluster_uri = self.cluster_config["name"]
        cluster_access = await self.aresource_access_level(token, cluster_uri)
        if cluster_access == ResourceAccess.WRITE:
            # if user has write access to cluster will have access to all resources
            return True

        if resource_uri is None and cluster_access not in [
            ResourceAccess.WRITE,
            ResourceAccess.READ,
        ]:
            # If module does not have a name, must have access to the cluster
            return False

        resource_access_level = await self.aresource_access_level(token, resource_uri)
        if resource_access_level not in [ResourceAccess.WRITE, ResourceAccess.READ]:
            return False

        return True

    async def aclear_auth_cache(self, token: str = None):
        self._auth_cache.clear_cache(token)

    ##############################################
    # Key to servlet where it is stored mapping
    ##############################################
    async def amark_env_servlet_name_as_initialized(self, env_servlet_name: str):
        self._initialized_env_servlet_names.add(env_servlet_name)

    async def ais_env_servlet_name_initialized(self, env_servlet_name: str) -> bool:
        return env_servlet_name in self._initialized_env_servlet_names

    async def aget_all_initialized_env_servlet_names(self) -> Set[str]:
        return self._initialized_env_servlet_names

    async def aget_key_to_env_servlet_name_dict_keys(self) -> List[Any]:
        return list(self._key_to_env_servlet_name.keys())

    async def aget_key_to_env_servlet_name_dict(self) -> Dict[Any, str]:
        return self._key_to_env_servlet_name

    async def aget_env_servlet_name_for_key(self, key: Any) -> str:
        self._last_activity = time.time()
        return self._key_to_env_servlet_name.get(key, None)

    async def aput_env_servlet_name_for_key(self, key: Any, env_servlet_name: str):
        if not await self.ais_env_servlet_name_initialized(env_servlet_name):
            raise ValueError(
                f"Env servlet name {env_servlet_name} not initialized, and you tried to mark a resource as in it."
            )
        self._key_to_env_servlet_name[key] = env_servlet_name

    async def apop_env_servlet_name_for_key(self, key: Any, *args) -> str:
        # *args allows us to pass default or not
        return self._key_to_env_servlet_name.pop(key, *args)

    async def aclear_key_to_env_servlet_name_dict(self):
        self._key_to_env_servlet_name = {}

    ##############################################
    # Remove Env Servlet
    ##############################################
    async def aclear_all_references_to_env_servlet_name(self, env_servlet_name: str):
        self._initialized_env_servlet_names.remove(env_servlet_name)
        deleted_keys = [
            key
            for key, env in self._key_to_env_servlet_name.items()
            if env == env_servlet_name
        ]
        for key in deleted_keys:
            self._key_to_env_servlet_name.pop(key)
        return deleted_keys

    ##############################################
    # Cluster status functions
    ##############################################

    async def asend_status_info_to_den(self):
        while True:
            logger.info("Sending cluster status to Den")
            try:
                interval_size = (await self.aget_cluster_config()).get(
                    "den_status_ping_interval"
                )
                if interval_size == -1:
                    break
                status: ResourceStatusData = await self.astatus()
                status_data = {
                    "status": "running",
                    "resource_type": status.cluster_config.get("resource_type"),
                    "data": dict(status),
                }
                cluster_uri = rns_client.format_rns_address(
                    (await self.aget_cluster_config()).get("name")
                )
                api_server_url = status.cluster_config.get(
                    "api_server_url", rns_client.api_server_url
                )
                post_status_data_resp = requests.post(
                    f"{api_server_url}/resource/{cluster_uri}/cluster/status",
                    data=json.dumps(status_data),
                    headers=rns_client.request_headers(),
                )
                if post_status_data_resp.status_code != 200:
                    logger.error(
                        f"({post_status_data_resp.status_code}) Failed to send cluster status check to Den: {post_status_data_resp.text}"
                    )
                else:
                    logger.info(
                        f"Successfully updated cluster status in Den. Next status check will be in {round(interval_size / 60, 2)} minutes."
                    )
            except Exception as e:
                logger.error(
                    f"Cluster status check has failed: {e}. Please check cluster logs for more info."
                )
                logger.warning(
                    f"Temporarily increasing the interval between two consecutive status checks. "
                    f"Next status check will be in {round(INCREASED_STATUS_CHECK_INTERVAL / 60, 2)} minutes. "
                    f"For changing the interval size, please restart the server with a new interval size value. "
                    f"If a value is not provided, interval size will be set to {DEFAULT_STATUS_CHECK_INTERVAL}"
                )
                await self.aset_cluster_config_value(
                    "den_status_ping_interval", INCREASED_STATUS_CHECK_INTERVAL
                )
                await asyncio.sleep(INCREASED_STATUS_CHECK_INTERVAL)
            finally:

                await asyncio.sleep(interval_size)

    def send_status_info_to_den(self):
        asyncio.run(self.asend_status_info_to_den())

    def schedule_post_status(self):
        # adding post status to den thread
        logger.debug("adding send_status_info_to_den to thread pool")
        # delay the start of post_status_thread, so we'll finish the cluster startup properly
        post_status_thread = threading.Timer(
            STATUS_CHECK_DELAY, self.send_status_info_to_den
        )
        logger.debug("starting send_status_info_to_den thread")

        post_status_thread.start()

    async def _cluster_status_helper(self, env_servlet_name):
        try:
            (
                objects_in_env_modified,
                env_utilization_data,
            ) = await obj_store.acall_actor_method(
                obj_store.get_env_servlet(env_servlet_name), method="status_local"
            )
            return {
                "env_servlet_name": env_servlet_name,
                "objects_in_env_modified": objects_in_env_modified,
                "env_utilization_data": env_utilization_data,
            }
        except ObjStoreError as e:
            return {"env_servlet_name": env_servlet_name, "Exception": e}

    async def astatus(self):
        import psutil

        from runhouse.utils import get_pid

        config_cluster = copy.deepcopy(self.cluster_config)

        # poping out creds because we don't want to show them in the status
        config_cluster.pop("creds", None)

        # getting cluster servlets (envs) and their related objects
        cluster_servlets = {}
        cluster_envs_env_utilization_data = {}
        get_env_servlets_status_tasks = [
            self._cluster_status_helper(env_servlet_name)
            for env_servlet_name in self._initialized_env_servlet_names
        ]
        env_servlets_status = await asyncio.gather(
            *get_env_servlets_status_tasks, return_exceptions=True
        )
        for env_status in env_servlets_status:
            env_servlet_name = env_status.get("env_servlet_name")
            if "Exception" in env_status.keys():
                cluster_servlets[env_servlet_name] = []
                cluster_envs_env_utilization_data[env_servlet_name] = {}
            else:
                cluster_servlets[env_servlet_name] = env_status.get(
                    "objects_in_env_modified"
                )
                cluster_envs_env_utilization_data[env_servlet_name] = env_status.get(
                    "env_utilization_data"
                )

        # TODO: decide if we need this info at all: cpu_usage, memory_usage, disk_usage
        cpu_usage = psutil.cpu_percent(interval=1)

        # Fields: `available`, `percent`, `used`, `free`, `active`, `inactive`, `buffers`, `cached`, `shared`, `slab`
        memory_usage = psutil.virtual_memory()._asdict()

        # Fields: `total`, `used`, `free`, `percent`
        disk_usage = psutil.disk_usage("/")._asdict()

        status_data = {
            "cluster_config": config_cluster,
            "runhouse_version": runhouse.__version__,
            "server_pid": get_pid(),
            "env_resource_mapping": cluster_servlets,
            "env_servlet_processes": cluster_envs_env_utilization_data,
            "system_cpu_usage": cpu_usage,
            "system_memory_usage": memory_usage,
            "system_disk_usage": disk_usage,
        }
        status_data = ResourceStatusData(**status_data)
        return status_data

    def status(self):
        return sync_function(self.astatus)()
