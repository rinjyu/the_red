from typing import Optional
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from exceptions import UnicornException

from model import Post
from rule import RangeShardPolicy
from utils import range_config_to_range_infos
from post import PostService

from log import init_log
from cors import init_cors
from instrumentator import init_instrumentator
from zoo import init_kazoo
from config import Config
from settings import Settings
from redis_conn import redis

import traceback
import json
import sys


def refresh_shard_range(data, stat):
    if not data:
        print("There is no data")
        return

    try:
        infos = range_config_to_range_infos(data)
        print(infos)
        policy = RangeShardPolicy(infos)
    except Exception as e:
        print(str(e))
        return None

    connections = {}
    for info in policy.infos:
        parts = info.host.split(':')
        conn = RedisConnection(f"{parts[1]}:{parts[2]}")
        connections[info.host] = conn

    global g_shardpolicy
    global g_connections
    g_shardpolicy = policy
    g_connections = connections
    print("Finished refresh_shard_range")


app = FastAPI()

ZK_DATA_PATH = "/the_red/storages/redis/shards/ranges"

g_shardpolicy = None
g_post_service = PostService()

my_settings = Settings()

conf = Config(my_settings.CONFIG_PATH)
init_log(app, conf.section("log")["path"])
init_cors(app)
init_instrumentator(app)
zk = init_kazoo(conf.section("zookeeper")["hosts"], ZK_DATA_PATH, refresh_shard_range, False)


@app.exception_handler(UnicornException)
async def unicorn_exception_handler(request: Request, exc: UnicornException):
    return JSONResponse(
        status_code=exc.status,
        content={"code": exc.code, "message": exc.message},
    )


@app.get("/api/v1/shards")
async def list_shards():
    global g_shardpolicy
    results = {} 
    i = 0
    for info in g_shardpolicy.infos:
        idx = str(i)
        results[idx] = {"start": info.start, "end": info.end, "host": info.host}
        i += 1

    return results


def get_conn_from_shard(key: int):
    global g_shardpolicy
    host = g_shardpolicy.getShardInfo(key)
    conn = g_connections[host].get_conn()

    print(key, host)
    return conn


@app.get("/api/v1/posts/{user_id}/")
async def lists(user_id: int, last: int = -1, limit: int = 10):
    conn = get_conn_from_shard(user_id)
    values, next_id = g_post_service.list(conn, user_id, limit, last)
    return {"data": values, "next": next_id}


@app.get("/api/v1/posts/{user_id}/{post_id}")
async def get_post(user_id: int, post_id: int):
    conn = get_conn_from_shard(user_id)
    post = g_post_service.get(conn, user_id, post_id)
    if not post:
        raise UnicornException(404, -10001, "No Post: {post_id}") 

    return {"post_id": post["post_id"], "contents": post["contents"]}


@app.get("/api/v1/write_post/{user_id}")
async def write_post(user_id: int, post_id: int, text: str):
    try:
        conn = get_conn_from_shard(user_id)
        post = g_post_service.write(conn, user_id, post_id, text)
        return {"post_id": post["post_id"], "contents": post["contents"]}
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        raise UnicornException(404, -10002, str(e)) 
