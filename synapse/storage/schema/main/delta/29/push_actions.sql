--
-- This file is licensed under the Affero General Public License (AGPL) version 3.
--
-- Copyright (C) 2023 New Vector, Ltd
--
-- This program is free software: you can redistribute it and/or modify
-- it under the terms of the GNU Affero General Public License as
-- published by the Free Software Foundation, either version 3 of the
-- License, or (at your option) any later version.
--
-- See the GNU Affero General Public License for more details:
-- <https://www.gnu.org/licenses/agpl-3.0.html>.
--
-- Originally licensed under the Apache License, Version 2.0:
-- <http://www.apache.org/licenses/LICENSE-2.0>.
--
-- [This file includes modifications made by New Vector Limited]
--
--
/* Copyright 2016 OpenMarket Ltd
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

ALTER TABLE event_push_actions ADD COLUMN topological_ordering BIGINT;
ALTER TABLE event_push_actions ADD COLUMN stream_ordering BIGINT;
ALTER TABLE event_push_actions ADD COLUMN notif SMALLINT;
ALTER TABLE event_push_actions ADD COLUMN highlight SMALLINT;

UPDATE event_push_actions SET stream_ordering = (
    SELECT stream_ordering FROM events WHERE event_id = event_push_actions.event_id
), topological_ordering = (
    SELECT topological_ordering FROM events WHERE event_id = event_push_actions.event_id
);

UPDATE event_push_actions SET notif = 1, highlight = 0;

/** Using CREATE INDEX directly is deprecated in favour of using background
 * update see synapse/storage/schema/delta/33/access_tokens_device_index.sql
 * and synapse/storage/registration.py for an example using
 * "access_tokens_device_index" **/
CREATE INDEX event_push_actions_rm_tokens on event_push_actions(
    user_id, room_id, topological_ordering, stream_ordering
);
