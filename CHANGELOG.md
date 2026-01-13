# Changelog

All notable changes to the Endpoint Comparison Tool will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [2.1.0] - 2026-01-13

### Added - Backend Improvements

#### API Resilience
- **Retry logic with exponential backoff** (`_fetch_with_retry` function)
  - Automatically retries failed API requests up to 3 times
  - Handles rate limiting (429 responses) with exponential backoff (1s, 2s, 4s)
  - Retries on server errors (5xx) with shorter intervals (0.5s, 1s, 2s)
  - Retries on timeout exceptions with progressive delays
  - Improves reliability when external APIs are temporarily unavailable

#### Enhanced Normalization
- **Logging for normalization transformations**
  - Added optional `log_transformations` parameter to `normalize_value()`
  - Logs each normalization step for debugging (encoding fixes, lowercase, suffix removal, etc.)
  - Helps troubleshoot hostname matching issues
  - Enable with `LOG_LEVEL=DEBUG` environment variable

- **Improved prefix matching logic**
  - Added minimum length threshold (10 characters) to prevent false positive matches
  - Prevents coincidental matches on very short hostnames
  - Logs all prefix matches for audit trail
  - Reduces incorrect device pairing in comparison results

#### Standardized Normalization
- **Unified normalization across codebase**
  - Client name matching now uses same `normalize_value()` function as device comparison
  - Ensures consistent matching behavior throughout the application
  - Eliminates discrepancies between client selection and device comparison

#### Adaptive Polling
- **Enhanced AD inventory polling**
  - Implements adaptive polling intervals: 2s, 2s, 4s, 4s, 8s, 8s... (capped at 8s)
  - Starts fast for quick results, slows down to reduce API load
  - Logs poll attempt count and timeout information
  - More efficient use of API resources while maintaining responsiveness

### Added - Frontend Improvements

#### Cache Management
- **Cache TTL (Time-To-Live) validation**
  - Automatically expires cached API responses after 1 hour
  - Displays cache age in console logs (e.g., "Cache hit for api_cache_S1_123 (age: 15min)")
  - Removes expired entries automatically on access
  - Prevents stale data from being used in comparisons

- **Cache size management**
  - Limits cache to 50 entries to prevent LocalStorage overflow
  - Automatically removes oldest cache entry when limit is reached
  - Handles QuotaExceededError gracefully by clearing all cache and retrying
  - Removes corrupted cache entries during cleanup

- **Automatic cache pruning**
  - Runs on page load to clean up expired/corrupted entries
  - Logs number of entries pruned
  - Keeps LocalStorage clean and performant

### Changed

#### Logging Improvements
- Enhanced debug logging throughout normalization pipeline
- Added informative log messages for:
  - API retry attempts with wait times
  - Rate limiting events
  - Cache operations (hit/miss/save/clear)
  - Prefix matching results
  - AD polling progress

#### Code Quality
- Improved error handling with more descriptive messages
- Better separation of concerns in normalization logic
- More consistent function signatures across codebase
- Enhanced code documentation with detailed comments

### Technical Details

#### Backend Changes (`app.py`)
- Added `_fetch_with_retry()` helper function (lines 56-105)
- Enhanced `normalize_value()` with logging parameter (lines 144-196)
- Improved prefix matching in `/compare` endpoint (lines 748-778)
- Standardized client matching in `/clients/unified` (lines 1292-1296)
- Adaptive polling in `/ad/trigger` endpoint (lines 1687-1696, 1779-1782)

#### Frontend Changes (`templates/index.html`)
- Enhanced `getCache()` with TTL validation (lines 2692-2716)
- Enhanced `setCache()` with size management (lines 2718-2772)
- Added `pruneExpiredCache()` function (lines 2780-2804)
- Automatic cache pruning on page load (line 5267)

### Performance Impact
- **Reduced API failures**: Retry logic handles transient errors automatically
- **Improved cache efficiency**: TTL validation prevents stale data usage
- **Better resource usage**: Adaptive polling reduces unnecessary API calls
- **Faster debugging**: Normalization logging helps identify matching issues quickly

### Migration Notes
- No breaking changes - all improvements are backward compatible
- Existing cache entries will be validated and pruned on first page load
- No configuration changes required
- Optional: Set `LOG_LEVEL=DEBUG` in `.env` to enable detailed normalization logging

### Recommendations
1. Monitor logs for retry patterns to identify API reliability issues
2. Adjust cache TTL if needed by modifying `maxAge` constant (currently 1 hour)
3. Review normalization logs if experiencing unexpected matching behavior
4. Consider increasing cache size limit if working with many clients frequently

---

## [2.0.0] - 2026-01-09

### Added
- Unified client selection modal
- Automatic matched client detection
- One-click client selection and comparison
- Real-time status updates during fetch/compare
- Source summary cards (endpoint/device counts)

### Removed
- Manual Excel file upload interface
- Drag-and-drop file zones
- Separate file cards for File 1 and File 2
- Individual SentinelOne/NinjaRMM source selection

### Fixed
- Quick Load button not working
- Case sensitivity in save config
- Back button not resetting view states properly

---

## [1.0.0] - 2026-01-12

### Added
- Initial release
- SentinelOne API integration
- NinjaRMM API integration
- Excel column comparison
- Automated remediation
- Active Directory inventory
- Basic Auth support
- Security hardening
