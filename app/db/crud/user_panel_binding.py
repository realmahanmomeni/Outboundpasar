from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models_oc import UserPanelBinding


async def list_user_panel_bindings(db: AsyncSession, user_id: int) -> list[UserPanelBinding]:
    stmt = (
        select(UserPanelBinding)
        .where(UserPanelBinding.user_id == user_id)
        .order_by(UserPanelBinding.id.asc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_user_panel_binding(
    db: AsyncSession, user_id: int, binding_id: int
) -> UserPanelBinding | None:
    stmt = select(UserPanelBinding).where(
        UserPanelBinding.id == binding_id,
        UserPanelBinding.user_id == user_id,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def create_user_panel_binding(db: AsyncSession, binding: UserPanelBinding) -> UserPanelBinding:
    db.add(binding)
    await db.flush()
    await db.refresh(binding)
    return binding


async def delete_user_panel_binding(db: AsyncSession, binding: UserPanelBinding) -> None:
    await db.delete(binding)
    await db.flush()
