"""One policy for fresh reads of a pool and of its member posts."""
from __future__ import annotations

import re

from sqlalchemy import func, literal_column, or_, select
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

from api.db.models import Pool, PoolPost, Post
from api.errors import KuraError

POST_FILE = re.compile(r"(?:posts/(?:custom-thumbnails/)?|generated-thumbnails/)([1-9][0-9]{0,9})_[^/]+")


def post_visible(post_id):
    memberships = (
        select(PoolPost.post_id)
        .join(Pool, Pool.pool_id == PoolPost.pool_id)
        .where(PoolPost.post_id == post_id).correlate_except(PoolPost, Pool)
    )
    return or_(~memberships.exists(), memberships.where(Pool.hidden.is_(False)).exists())


def post_visible_sql(expression):
    """SQL for static, trusted aliases used by read-model queries.

    Always parenthesized: the clause is ``NOT EXISTS(..) OR EXISTS(..)``, and callers
    splice it after ``AND`` — unparenthesized, ``A AND NOT X OR Y`` became
    ``(A AND NOT X) OR Y``, scanning every post (4 s per review-set load) and
    admitting posts outside the caller's filter."""
    return "(" + str(post_visible(literal_column(expression))) + ")"


async def pool_covers(db, pool_ids, *, safe=False):
    """Resolve gallery covers in one read, preserving an available preferred cover."""
    if not pool_ids:
        return {}
    eligibility = [Post.status == "ok", post_visible(Post.post_id)]
    if safe:
        eligibility.append(Post.safety == "safe")
    preferred = (select(Post.post_id)
                 .where(Post.post_id == Pool.cover_post_id, *eligibility)
                 .correlate(Pool).scalar_subquery())
    fallback = (select(Post.post_id).select_from(PoolPost).join(Post)
                .where(PoolPost.pool_id == Pool.pool_id, *eligibility)
                .order_by(PoolPost.order, Post.post_id).limit(1)
                .correlate(Pool).scalar_subquery())
    rows = await db.execute(
        select(Pool.pool_id, Post.post_id, Post.source)
        .select_from(Pool).join(Post, Post.post_id == func.coalesce(preferred, fallback))
        .where(Pool.pool_id.in_(pool_ids), Pool.hidden.is_(False))
    )
    return {pool_id: (post_id, source) for pool_id, post_id, source in rows}


async def require_visible_post(db, post_id):
    if not (await db.execute(select(post_visible(post_id)))).scalar_one():
        raise KuraError(404, "post_not_found", fix="Check the post ID and its pool visibility.")


class VisibleMediaFiles(StaticFiles):
    async def get_response(self, path, scope):
        match = POST_FILE.fullmatch(path)
        if match:
            from api.db.session import async_session

            post_id = int(match[1])
            if post_id >= 2**31:
                return Response(status_code=404)
            async with async_session() as db:
                await require_visible_post(db, post_id)
        response = await super().get_response(path, scope)
        if match:
            response.headers["Cache-Control"] = "private, no-cache"
        return response
