"""Shared run-source identifiers and literal-prefix escaping."""


def _like_prefix(prefix: str) -> str:
    """把用户输入转成只按前缀匹配的 LIKE 模式（配合 ESCAPE '!'）。

    不转义的话 prefix 里的 % / _ 会被当通配符，调用方拿到的就不是它要的那批。
    """
    escaped = (
        prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    )
    return escaped + "%"


# 调用来源靠 context_id 前缀区分：管理台试运行与压测各自生成带前缀的
# context，其余（业务方经 /a2a/{slug} 打进来的）没有前缀。这不是强约定，
# 只是这两处生成 context 时的命名（见 test_run_master / perf.runner）。
RUN_SOURCE_PREFIXES = {"testrun": "testrun-", "perf": "perf-"}
