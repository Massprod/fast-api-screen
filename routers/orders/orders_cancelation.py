from bson import ObjectId
from loguru import logger
from fastapi import HTTPException, status
from motor.motor_asyncio import AsyncIOMotorClient
from routers.base_platform.crud import db_get_platform_cell_data, db_update_platform_cell_data
from routers.grid.crud import (db_get_grid_cell_data,
                               db_get_grid_extra_cell_data,
                               db_update_grid_cell_data,
                               db_delete_extra_cell_order)
from routers.wheelstacks.crud import db_find_wheelstack_by_object_id, db_update_wheelstack
from routers.orders.crud import db_delete_order, db_create_order
from constants import (DB_PMK_NAME, CLN_ACTIVE_ORDERS, CLN_GRID, CLN_WHEELSTACKS,
                       ORDER_STATUS_CANCELED, CLN_CANCELED_ORDERS, PRES_TYPE_GRID,
                       PRES_TYPE_PLATFORM, CLN_BASE_PLATFORM)
from utility.utilities import time_w_timezone


async def orders_cancel_basic_extra_element_moves(
        order_data: dict,
        cancellation_reason: str,
        db: AsyncIOMotorClient,
):
    # Only GRID can be a source for `extra` elements
    # -1- <- Check source cell it should exist.
    source_id: ObjectId = order_data['source']['placementId']
    source_row: str = order_data['source']['rowPlacement']
    source_col: str = order_data['source']['columnPlacement']
    source_cell_data = await db_get_grid_cell_data(
        source_id, source_row, source_col, db, DB_PMK_NAME, CLN_GRID
    )
    # TODO: This extra checks should delete order and clear Corrupted data.
    #  But all of these is too much for now.
    #  We need to add some clearing process for all of these corruption options, and catch this exceptions.
    if source_cell_data is None:
        logger.error(f'{source_row}|{source_col} <- source cell doesnt exist in the `grid` = {source_id}'
                     f'But given order = {order_data['_id']} marks it as source cell.')
        raise HTTPException(
            detail=f'{source_row}|{source_col} <- source cell doesnt exist in the `grid` = {source_id}'
                   f'But given order = {order_data['_id']} marks it as source cell.',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    source_cell_data = source_cell_data['rows'][source_row]['columns'][source_col]
    if source_cell_data['blockedBy'] != order_data['_id']:
        logger.error(f'Corrupted `order` = {order_data['_id']},'
                     f' marking cell {source_row}|{source_col} in `grid` {source_id}.'
                     f'But different order is blocking it {source_cell_data['blockedBy']}')
        raise HTTPException(
            detail=f'Corrupted `order` = {order_data['_id']},'
                   f' marking cell {source_row}|{source_col} in `grid` {source_id}.'
                   f'But different order is blocking it {source_cell_data['blockedBy']}',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    # -2- <- Check source wheelStack it should exist.
    source_wheelstack_data = await db_find_wheelstack_by_object_id(
        source_cell_data['wheelStack'], db, DB_PMK_NAME, CLN_WHEELSTACKS
    )
    if source_wheelstack_data is None:
        logger.error(
            f'Corrupted cell {source_row}|{source_col} in grid = {source_id}.'
            f'Marks `wheelStacks` {source_cell_data['wheelStack']} as placed on it, but it doesnt exist.'
        )
        raise HTTPException(
            detail=f'Corrupted cell {source_row}|{source_col} in grid = {source_id}.'
                   f'Marks `wheelStacks` {source_cell_data['wheelStack']} as placed on it, but it doesnt exist.',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    # -3- <- Check destination it should exist.
    dest_id = order_data['destination']['placementId']
    dest_extra_row = order_data['destination']['rowPlacement']
    dest_element_name = order_data['destination']['columnPlacement']
    destination_element_data = await db_get_grid_extra_cell_data(
        dest_id, dest_element_name, db, DB_PMK_NAME, CLN_GRID
    )
    if destination_element_data is None:
        logger.error(
            f'Corrupted extra element in `grid` = {dest_id}, cell {dest_extra_row}|{dest_element_name}.'
            f'Used in order = {order_data['_id']}, but it doesnt exist.'
        )
        raise HTTPException(
            detail=f'Corrupted extra element cell {dest_extra_row}|{dest_element_name}.'
                   f'Used in order = {order_data['_id']}, but it doesnt exist.',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    # -4- <- Unblock source
    source_cell_data['blocked'] = False
    source_cell_data['blockedBy'] = None
    # TODO: Search for transactions in MongoDB, and change all write operations.
    #  We should always update and create with transactions, when it's multiple documents.
    await db_update_grid_cell_data(
        source_id, source_row, source_col, source_cell_data, db, DB_PMK_NAME, CLN_GRID
    )
    # -5- <- Delete order from destination extra element
    await db_delete_extra_cell_order(
        dest_id, dest_element_name, order_data['_id'], db, DB_PMK_NAME, CLN_GRID
    )
    # -6- <- Unblock `wheelStack` and update `lastOrder`.
    source_wheelstack_data['blocked'] = False
    source_wheelstack_data['lastOrder'] = order_data['_id']
    await db_update_wheelstack(
        source_wheelstack_data, source_wheelstack_data['_id'], db, DB_PMK_NAME, CLN_WHEELSTACKS
    )
    # -7- Delete order from `activeOrders`
    await db_delete_order(
        order_data['_id'], db, DB_PMK_NAME, CLN_ACTIVE_ORDERS
    )
    # -8- Add order into `canceledOrders`
    cancellation_time = await time_w_timezone()
    order_data['status'] = ORDER_STATUS_CANCELED
    order_data['cancellationReason'] = cancellation_reason if cancellation_reason else 'Not specified'
    order_data['canceledAt'] = cancellation_time
    order_data['lastUpdated'] = cancellation_time
    canceled_order = await db_create_order(
        order_data, db, DB_PMK_NAME, CLN_CANCELED_ORDERS
    )
    return canceled_order.inserted_id


async def orders_cancel_move_wholestack(
        order_data: dict,
        cancellation_reason: str,
        db: AsyncIOMotorClient,
):
    # `grid` and `basePlatform` can be used as a source for `moveWholeStack`.
    # -1- <- Check source cell it should exist.
    source_type: str = order_data['source']['placementType']
    source_id: ObjectId = order_data['source']['placementId']
    source_row: str = order_data['source']['rowPlacement']
    source_col: str = order_data['source']['columnPlacement']
    source_cell_data = None
    if PRES_TYPE_GRID == source_type:
        source_cell_data = await db_get_grid_cell_data(
            source_id, source_row, source_col, db, DB_PMK_NAME, CLN_GRID
        )
    elif PRES_TYPE_PLATFORM == source_type:
        source_cell_data = await db_get_platform_cell_data(
            source_id, source_row, source_col, db, DB_PMK_NAME, CLN_BASE_PLATFORM
        )
    if source_cell_data is None:
        logger.error(f'{source_row}|{source_col} <- source cell doesnt exist in the `{source_type}` = {source_id}'
                     f'But given order = {order_data['_id']} marks it as source cell.')
        raise HTTPException(
            detail=f'{source_row}|{source_col} <- source cell doesnt exist in the `{source_type}` = {source_id}'
                   f'But given order = {order_data['_id']} marks it as source cell.',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    source_cell_data = source_cell_data['rows'][source_row]['columns'][source_col]
    if source_cell_data['blockedBy'] != order_data['_id']:
        logger.error(f'Corrupted `order` = {order_data['_id']},'
                     f' marking cell {source_row}|{source_col} in `grid` {source_id}.'
                     f'But different order is blocking it {source_cell_data['blockedBy']}')
        raise HTTPException(
            detail=f'Corrupted `order` = {order_data['_id']},'
                   f' marking cell {source_row}|{source_col} in `grid` {source_id}.'
                   f'But different order is blocking it {source_cell_data['blockedBy']}',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
    # -2- <- Check source `wheelStack` it should exist.
    source_wheelstack_data = await db_find_wheelstack_by_object_id(
        source_cell_data['wheelStack'], db, DB_PMK_NAME, CLN_WHEELSTACKS
    )
    if source_wheelstack_data is None:
        logger.error(
            f'Corrupted cell {source_row}|{source_col} in grid = {source_id}.'
            f'Marks `wheelStacks` {source_cell_data['wheelStack']} as placed on it, but it doesnt exist.'
        )
        raise HTTPException(
            detail=f'Corrupted cell {source_row}|{source_col} in grid = {source_id}.'
                   f'Marks `wheelStacks` {source_cell_data['wheelStack']} as placed on it, but it doesnt exist.',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    # Destination on another hand can only be a `grid`
    # -3- <- Check destination it should exist.
    dest_id = order_data['destination']['placementId']
    dest_row = order_data['destination']['rowPlacement']
    dest_col = order_data['destination']['columnPlacement']
    destination_cell_data = await db_get_grid_cell_data(
        dest_id, dest_row, dest_col, db, DB_PMK_NAME, CLN_GRID
    )
    if destination_cell_data is None:
        logger.error(
            f'Corrupted `grid` = {dest_id}  cell {dest_row}|{dest_col}.'
            f'Used in order = {order_data['_id']}, but it doesnt exist.'
        )
        raise HTTPException(
            detail=f'Corrupted extra element cell {dest_row}|{dest_col}.'
                   f'Used in order = {order_data['_id']}, but it doesnt exist.',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    destination_cell_data = destination_cell_data['rows'][dest_row]['columns'][dest_col]
    if destination_cell_data['wheelStack'] is not None:
        logger.error(
            f'Corrupted `grid` = {dest_id} cell {dest_row}|{dest_col}.'
            f'Used in order = {order_data['_id']} as destination, but its already taken'
        )
        raise HTTPException(
            detail=f'Corrupted `grid` = {dest_id} cell {dest_row}|{dest_col}.'
                   f'Used in order = {order_data['_id']} as destination, but its already taken',
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    # -4- Unblock source
    source_cell_data['blocked'] = False
    source_cell_data['blockedBy'] = None
    if PRES_TYPE_GRID == source_type:
        await db_update_grid_cell_data(
            source_id, source_row, source_col, source_cell_data, db, DB_PMK_NAME, CLN_GRID
        )
    elif PRES_TYPE_PLATFORM == source_type:
        await db_update_platform_cell_data(
            source_id, source_row, source_col, source_cell_data, db, DB_PMK_NAME, CLN_BASE_PLATFORM
        )
    # -5- Unblock destination
    destination_cell_data['blocked'] = False
    destination_cell_data['blockedBy'] = None
    await db_update_grid_cell_data(
        dest_id, dest_row, dest_col, destination_cell_data, db, DB_PMK_NAME, CLN_GRID
    )
    # -6- Unblock Source `wheelStack`
    source_wheelstack_data['blocked'] = False
    source_wheelstack_data['lastOrder'] = order_data['_id']
    await db_update_wheelstack(
        source_wheelstack_data, source_wheelstack_data['_id'], db, DB_PMK_NAME, CLN_WHEELSTACKS
    )
    # -7- Delete order from `activeOrders`
    await db_delete_order(
        order_data['_id'], db, DB_PMK_NAME, CLN_ACTIVE_ORDERS
    )
    # -7- Add order into `canceledOrders`
    cancellation_time = await time_w_timezone()
    order_data['status'] = ORDER_STATUS_CANCELED
    order_data['cancellationReason'] = cancellation_reason if cancellation_reason else 'Not specified'
    order_data['canceledAt'] = cancellation_time
    order_data['lastUpdated'] = cancellation_time
    canceled_order = await db_create_order(
        order_data, db, DB_PMK_NAME, CLN_CANCELED_ORDERS
    )
    return canceled_order.inserted_id
