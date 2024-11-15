import asyncio

import msgspec
from redis.asyncio.client import Redis

from webkit.throttle.decorator import LimitRule


class Limiter:
    """
    Rate limiter implementation using Redis for distributed rate limiting.
    """

    LUA_SCRIPT = """
    -- Retrieve arguments
    -- ruleset = {key: [limit, window_size], ...}
    -- return = {key: [limit, current], ...}
    local now = tonumber(ARGV[1])
    local ruleset = cjson.decode(ARGV[2])

    for i, key in ipairs(KEYS) do
        -- Step 1: Remove expired requests from the sorted set
        redis.call('ZREMRANGEBYSCORE', key, 0, now - ruleset[key][2])

        -- Step 2: Count the number of requests within the valid time window
        local amount = redis.call('ZCARD', key)

        -- Step 3: Add the current request timestamp to the sorted set
        if amount < ruleset[key][1] then
            redis.call('ZADD', key, now, tostring(now))
            amount = amount + 1
        end

        -- Step 4: Set the TTL for the key
        redis.call("EXPIRE", key, ruleset[key][2])
        ruleset[key][2] = amount
        ruleset[key][3] = redis.call("ZRANGE", key, -1, -1)[1]
    end

    return cjson.encode(ruleset)
    """

    def __init__(self, redis: Redis):
        """
        :param redis: Redis client instance
        """

        self._redis = redis
        self._redis_function = self._redis.register_script(Limiter.LUA_SCRIPT)

    @staticmethod
    def _get_ruleset(identifier: str, rules: list[LimitRule]) -> dict[str, tuple[int, int]]:
        """
        Constructs a ruleset dictionary mapping keys to limits and intervals.

        :param identifier: User or session identifier
        :param rules: List of rate limit rules to apply
        :return: Dictionary of {key: (max_requests, interval)}
        """

        keys = map(lambda rule: identifier + rule.throttle_key, rules)
        args = map(lambda rule: (rule.max_requests, rule.interval), rules)

        return dict(zip(keys, args))

    async def _get_limits(self, ruleset) -> dict[str, list[int, int]]:
        """
        Executes the rate limiting Lua script in Redis.

        :param ruleset: Dictionary of rate limit rules
        :return: Dictionary of updated counts and timestamps
        """

        now = asyncio.get_running_loop().time()

        result = await self._redis_function(keys=list(ruleset.keys()), args=[now, msgspec.json.encode(ruleset)])
        result = msgspec.json.decode(result)

        return result

    async def is_deny(self, identifier: str, rules: list[LimitRule]) -> list[float]:
        """
        Checks if any rate limits are exceeded.

        :param identifier: User or session identifier
        :param rules: List of rate limit rules to check
        :return: List of waiting times until rate limits reset (empty if not exceeded)
        """

        ruleset = self._get_ruleset(identifier, rules)

        result = await self._get_limits(ruleset)
        now = asyncio.get_running_loop().time()
        deny = [float(val[2]) + ruleset[key][1] - now for key, val in result.items() if val[0] <= val[1]]

        return deny
