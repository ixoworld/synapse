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
/* Copyright 2014-2016 OpenMarket Ltd
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

CREATE TABLE IF NOT EXISTS state_groups(
    id BIGINT PRIMARY KEY,
    room_id TEXT NOT NULL,
    event_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS state_groups_state(
    state_group BIGINT NOT NULL,
    room_id TEXT NOT NULL,
    type TEXT NOT NULL,
    state_key TEXT NOT NULL,
    event_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_to_state_groups(
    event_id TEXT NOT NULL,
    state_group BIGINT NOT NULL,
    UNIQUE (event_id)
);

CREATE INDEX state_groups_id ON state_groups(id);

CREATE INDEX state_groups_state_id ON state_groups_state(state_group);
CREATE INDEX state_groups_state_tuple ON state_groups_state(room_id, type, state_key);
CREATE INDEX event_to_state_groups_id ON event_to_state_groups(event_id);
