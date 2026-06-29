"""
VK worker package: stateless workers that pull jobs from Redis, call VK API,
and push results back to Redis. All rate limiting and queueing lives in Redis.
"""

