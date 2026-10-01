"""拖轮/引航员资源表读取。

正常情况下从持久化的资源目录读取；测试/上游故障时可通过 fail_times
注入若干次 ResourceUnavailable，用来验证「读取失败保留已受理批次」。
"""
from typing import Any, Dict

from .repository import ResourceUnavailable


class ResourceProvider:
    def __init__(self, repository: Any) -> None:
        self.repository = repository
        self._fail_times = 0

    def fail_times(self, times: int) -> None:
        self._fail_times = max(0, int(times))

    def fetch(self) -> Dict[str, Any]:
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ResourceUnavailable("拖轮/引航员资源表读取失败")
        resources = self.repository.state().get("resources")
        if not resources or not resources.get("tugboats") or not resources.get("pilots"):
            raise ResourceUnavailable("拖轮/引航员资源尚未配置")
        return resources
