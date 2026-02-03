CREATE TABLE IF NOT EXISTS codeids (
    id SERIAL PRIMARY KEY,
    codeid TEXT UNIQUE NOT NULL  -- Unique CodeID value
);

CREATE TABLE IF NOT EXISTS effective_movement (
    id SERIAL PRIMARY KEY,
    codeid_id INT REFERENCES codeids(id),  -- Foreign key to codeids
    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time   TIMESTAMP WITH TIME ZONE NOT NULL,
    duration   NUMERIC NOT NULL,
    leg        TEXT NOT NULL  -- 'Left' or 'Right'
);

-- Indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_effective_movement_codeid_id
    ON effective_movement(codeid_id);
CREATE INDEX IF NOT EXISTS idx_effective_movement_leg
    ON effective_movement(leg);

CREATE TABLE IF NOT EXISTS activity_leg (
    id SERIAL PRIMARY KEY,
    codeid_id  INT REFERENCES codeids(id),  -- Foreign key to codeids
    foot       TEXT NOT NULL,               -- 'Left' or 'Right'
    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time   TIMESTAMP WITH TIME ZONE NOT NULL,
    duration   NUMERIC NOT NULL,            -- Window duration in seconds
    mac        TEXT,                        -- Device MAC address
    device_name TEXT,                       -- Device name
    total_value NUMERIC                     -- Aggregated value over window
);

CREATE TABLE IF NOT EXISTS activity_all (
    id            SERIAL PRIMARY KEY,
    codeid_ids    INTEGER[] NOT NULL,  -- Array of codeid_ids ([Left, Right])
    codeleg_ids   INTEGER[] NOT NULL,  -- Array of activity_leg IDs ([Left, Right])
    start_time    TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time      TIMESTAMP WITH TIME ZONE NOT NULL,
    duration      NUMERIC NOT NULL,
    macs          TEXT[],             -- Array of MAC addresses ([Left, Right])
    device_names  TEXT[],             -- Array of device names ([Left, Right])
    active_legs   TEXT[],             -- Array of legs (['Left','Right'])
    is_effective  BOOLEAN DEFAULT FALSE  -- Flag indicating effective gait
);

CREATE TABLE IF NOT EXISTS fullref_sensor_codeid (
    id          SERIAL PRIMARY KEY,
    codeid_id   INT REFERENCES codeids(id),
    foot        TEXT NOT NULL,       -- 'Left' or 'Right'
    device_name TEXT,                -- Device name
    mac         TEXT,                -- Device MAC address
    start_time  TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time    TIMESTAMP WITH TIME ZONE NOT NULL
);

-- Indexes for faster lookups
CREATE INDEX IF NOT EXISTS idx_fullref_codeid
    ON fullref_sensor_codeid(codeid_id);
CREATE INDEX IF NOT EXISTS idx_fullref_foot
    ON fullref_sensor_codeid(foot);
CREATE INDEX IF NOT EXISTS idx_fullref_time
    ON fullref_sensor_codeid(start_time, end_time);

-- Table for overlapping (simultaneous) effective-gait periods
CREATE TABLE IF NOT EXISTS effective_gait (
    id         SERIAL PRIMARY KEY,
    codeid_id  INT REFERENCES codeids(id),
    start_time TIMESTAMP WITH TIME ZONE NOT NULL,
    end_time   TIMESTAMP WITH TIME ZONE NOT NULL,
    duration   NUMERIC NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_effective_gait_codeid
    ON effective_gait(codeid_id);
