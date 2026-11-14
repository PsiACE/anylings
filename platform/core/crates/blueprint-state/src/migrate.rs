//! Migration runner. Forward-only and idempotent: every migration is recorded once,
//! and a database whose version is already current is left untouched.

use super::{SCHEMA_VERSION, StateError, Store};

/// Migrations in order. Index 0 upgrades version 0 to version 1.
const MIGRATIONS: [&str; 1] = [include_str!("../migrations/0001_initial.sql")];

impl Store {
    pub(super) fn migrate(&mut self) -> Result<(), StateError> {
        self.connection()
            .execute_batch(
                "CREATE TABLE IF NOT EXISTS schema_migrations (\
                 version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
            )
            .map_err(StateError::from)?;

        let current = self.schema_version()?;
        if current > SCHEMA_VERSION {
            return Err(StateError::SchemaVersion {
                found: current,
                expected: SCHEMA_VERSION,
            });
        }
        for (index, sql) in MIGRATIONS.iter().enumerate() {
            let version = index as i64 + 1;
            if version <= current {
                continue;
            }
            let tx = self.connection_mut().transaction()?;
            tx.execute_batch(sql)?;
            tx.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?1, ?2)",
                rusqlite::params![version, chrono_like_now()],
            )?;
            tx.commit()?;
        }
        Ok(())
    }
}

/// Timestamp for migration bookkeeping. Kept in SQLite's own text form so the
/// stored value sorts the same way the log does.
pub(super) fn chrono_like_now() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    format_utc_seconds(seconds)
}

/// Format epoch seconds as `YYYY-MM-DDTHH:MM:SSZ` without a date-time dependency.
pub fn format_utc_seconds(seconds: u64) -> String {
    let days = seconds / 86_400;
    let rem = seconds % 86_400;
    let (year, month, day) = civil_from_days(days as i64);
    format!(
        "{year:04}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        rem / 3600,
        (rem % 3600) / 60,
        rem % 60
    )
}

/// Howard Hinnant's civil-from-days, the standard exact conversion.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = (z - era * 146_097) as u64;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe as i64 + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let m = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (if m <= 2 { y + 1 } else { y }, m, d)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn epoch_zero_formats_as_the_unix_epoch() {
        assert_eq!(format_utc_seconds(0), "1970-01-01T00:00:00Z");
    }

    #[test]
    fn a_known_timestamp_round_trips_its_calendar_fields() {
        assert_eq!(format_utc_seconds(1_700_000_000), "2023-11-14T22:13:20Z");
    }

    #[test]
    fn leap_day_is_represented() {
        assert_eq!(format_utc_seconds(1_709_164_800), "2024-02-29T00:00:00Z");
    }
}
