"""Which automated tests provide evidence for each Phase 6 requirement, and of what kind.

Evidence kinds (kept separate in the test report, as required):

* ``mock``  - simulated source data or simulated Windows disk answers (in-memory
              backend, fake disk API) and injected faults. Proves logic, not hardware.
* ``image`` - real macOS-created APFS images read through the real libfsapfs backend
              (some with an injected fault, e.g. a reader that "unplugs").
* ``os``    - real Windows behaviour on this PC (NTFS, ACLs, handles, junctions,
              disk counters) with any source.
* ``hardware`` - the physical APFS SSD: ``hardware-results.json`` written by
              ``python -m apfs_explorer_lite.hwtest``, and the opt-in GUI test
              ``tests/test_live_ssd.py`` (counted only when it actually ran, not skipped).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Requirement:
    number: int
    title: str
    needs_hardware: bool  # only a physical-SSD run can fully confirm it
    note: str = ""


REQUIREMENTS = [
    Requirement(1, "External APFS SSD detection", True),
    Requirement(2, "Correct APFS partition identification", True),
    Requirement(3, "Read-only source access", True),
    Requirement(4, "Real directory enumeration", True),
    Requirement(5, "Small file copying", False),
    Requirement(6, "Large file copying", True, "No public APFS image contains a large file."),
    Requirement(7, "Complete folder copying", False),
    Requirement(8, "Nested folder copying", False),
    Requirement(9, "Files with Unicode names", False),
    Requirement(10, "Windows-incompatible filenames", False),
    Requirement(11, "Duplicate destination filenames", False),
    Requirement(12, "SSD disconnection during copying", True, "Physical unplug is a manual, optional step."),
    Requirement(13, "Insufficient destination storage", False, "A real full disk is simulated, not produced."),
    Requirement(14, "Access-denied errors", False),
    Requirement(15, "Application cancellation", False),
    Requirement(16, "Copy integrity verification", False),
    Requirement(17, "Large directory performance", True, "Real-drive listing speed depends on the SSD and USB link."),
    Requirement(18, "GUI responsiveness", False),
    Requirement(19, "Resource cleanup and file-handle closure", False),
    Requirement(20, "No accidental source writes", True),
]

# (test file::function pattern, evidence kind, requirement numbers)
TEST_MAP: list[tuple[str, str, tuple[int, ...]]] = [
    # 1-2 detection and identification
    ("test_discovery.py::test_confirms_apfs_on_external_usb_disk", "mock", (1, 2)),
    ("test_discovery.py::test_hotplug_nvme_disk_with_apfs_is_candidate", "mock", (1,)),
    ("test_discovery.py::test_classify_external", "mock", (1,)),
    ("test_discovery.py::test_ntfs_and_mbr_usb_disks_are_not_candidates", "mock", (1, 2)),
    ("test_discovery.py::test_uninitialized_disk_is_not_candidate", "mock", (1,)),
    ("test_discovery.py::test_disk_disconnected_during_*", "mock", (1,)),
    ("test_logging_and_gui.py::test_refresh_lists_disk_partitions_and_container", "mock", (1,)),
    ("test_live_windows.py::test_live_scan_is_consistent", "os", (1,)),
    ("test_discovery.py::test_unrecognized_partition_type_is_not_assumed_apfs", "mock", (2,)),
    ("test_discovery.py::test_apfs_type_without_superblock_is_hidden_with_warning", "mock", (2,)),
    ("test_discovery.py::test_bad_checksum_gives_tentative", "mock", (2,)),
    ("test_discovery.py::test_overwritten_block0_is_recoverable", "mock", (2,)),
    ("test_apfs_signature.py::*", "mock", (2,)),
    ("test_apfs_signature.py::test_libfsapfs_agrees_on_signature_location", "image", (2,)),
    ("test_real_apfs_images.py::test_gpt_parser_on_real_image", "image", (2,)),
    ("test_real_apfs_images.py::test_wrong_offset_is_not_an_apfs_container", "image", (2,)),
    ("test_overwritten_block0.py::*", "image", (2,)),
    # 3 read-only access, 20 no writes
    ("test_win32_safety.py::test_only_query_and_read_access_allowed", "mock", (3, 20)),
    ("test_win32_safety.py::test_open_with_write_access_is_blocked_before_windows", "os", (3, 20)),
    ("test_win32_safety.py::test_non_allowlisted_ioctl_is_blocked_before_windows", "os", (3, 20)),
    ("test_win32_safety.py::test_allowlisted_ioctls_*", "mock", (3, 20)),
    ("test_win32_safety.py::test_modifying_ioctls_are_not_allowlisted", "mock", (3, 20)),
    ("test_win32_safety.py::test_source_tree_has_no_write_primitives", "mock", (20,)),
    ("test_win32_safety.py::test_disk_counters_readable_without_write_access", "os", (20,)),
    ("test_safety.py::test_write_modes_blocked", "mock", (3, 20)),
    ("test_backends.py::test_backend_interface_has_no_write_operations", "mock", (3, 20)),
    ("test_backends.py::test_registry_rejects_write_capable_backend", "mock", (3, 20)),
    ("test_source_io.py::test_window_has_no_write_interface", "mock", (3, 20)),
    ("test_source_io.py::test_win32_rejects_unexpected_open_flags", "os", (3,)),
    ("test_real_apfs_images.py::test_reads_are_aligned_and_bounded_to_partition", "image", (3, 20)),
    ("test_resources.py::test_browse_and_copy_from_read_only_image", "image", (3, 20)),
    ("test_copy_engine.py::test_copy_from_container_with_damaged_first_block", "image", (20,)),
    ("test_overwritten_block0.py::test_overlay_reader_patches_only_in_memory", "image", (20,)),
    # 4 enumeration
    ("test_real_apfs_images.py::test_list_*", "image", (4,)),
    ("test_real_apfs_images.py::test_nested_navigation", "image", (4, 8)),
    ("test_real_apfs_images.py::test_file_metadata", "image", (4,)),
    ("test_explorer.py::test_open_lists_real_root_folder", "image", (4,)),
    ("test_explorer.py::test_tree_loads_lazily_and_navigates", "image", (4,)),
    ("test_copy_engine.py::test_plan_order_and_names", "image", (4, 8)),
    # 5-8 copying
    ("test_copy_engine.py::test_copy_one_file", "image", (5,)),
    ("test_copy_engine.py::test_copy_multiple_items_and_one_folder", "image", (5, 7)),
    ("test_copy_engine.py::test_copy_through_windows_unbuffered_reader", "image", (5, 7)),
    ("test_copy_engine.py::test_empty_file_and_other_volume", "image", (5,)),
    ("test_copy_dialog.py::test_copy_files_and_folder_end_to_end", "image", (5, 7)),
    ("test_copy_engine.py::test_large_file_is_streamed_with_bounded_memory", "mock", (6,)),
    ("test_real_apfs_images.py::test_large_chunk_read_and_hash", "image", (6,)),
    ("test_copy_engine.py::test_copy_whole_volume_preserves_hierarchy", "image", (7, 8)),
    ("test_copy_engine.py::test_copy_from_subfolder_maps_relative_to_it", "image", (8,)),
    ("test_copy_engine.py::test_long_destination_paths", "os", (8, 10)),
    ("test_copy_engine.py::test_timestamps_preserved", "image", (7,)),
    # 9-11 names
    ("test_reliability.py::test_unicode_names_are_preserved", "mock", (9,)),
    ("test_reliability.py::test_names_that_ntfs_may_merge_never_lose_a_file", "os", (9, 11)),
    ("test_reliability.py::test_invalid_unicode_name_fails_alone_and_report_still_saves", "mock", (9,)),
    ("test_copy_engine.py::test_invalid_and_colliding_names", "os", (10, 11)),
    ("test_safety.py::test_sanitize_*", "mock", (10,)),
    ("test_safety.py::test_numbered_name*", "mock", (10, 11)),
    ("test_copy_engine.py::test_conflict_*", "image", (11,)),
    ("test_copy_engine.py::test_file_in_the_way_of_folder_gets_new_folder_name", "image", (11,)),
    ("test_copy_engine.py::test_replace_refused_for_folder_in_the_way", "image", (11,)),
    ("test_copy_dialog.py::test_conflict_question_is_asked_on_gui_thread", "image", (11,)),
    ("test_copy_dialog.py::test_skip_policy_from_combo", "image", (11,)),
    # 12 disconnection
    ("test_copy_engine.py::test_drive_unplugged_*", "image", (12,)),
    ("test_copy_dialog.py::test_drive_unplugged_during_copy", "image", (12,)),
    ("test_real_apfs_images.py::test_disconnect_during_reading_is_reported", "image", (12,)),
    ("test_explorer.py::test_unplugged_drive_is_reported", "image", (12,)),
    # 13 storage
    ("test_copy_engine.py::test_not_enough_space_stops_before_writing", "image", (13,)),
    ("test_copy_engine.py::test_disk_full_while_writing", "image", (13,)),
    ("test_reliability.py::test_file_too_large_for_fat32_fails_that_file_only", "image", (13,)),
    ("test_reliability.py::test_destination_file_system_is_detected", "os", (13,)),
    # 14 access denied
    ("test_reliability.py::test_unwritable_destination_stops_with_one_clear_message", "os", (14,)),
    ("test_reliability.py::test_inaccessible_destination_is_explained", "os", (14,)),
    ("test_reliability.py::test_replace_of_*", "os", (14, 11)),
    ("test_reliability.py::test_source_disk_without_admin_is_access_denied", "os", (14,)),
    ("test_copy_engine.py::test_one_failed_file_does_not_stop_the_rest", "image", (14,)),
    ("test_discovery.py::test_access_denied_gives_tentative_with_admin_hint", "mock", (14,)),
    ("test_discovery.py::test_metadata_access_denied_on_one_disk_is_reported", "mock", (14,)),
    ("test_safety.py::test_system_folders_rejected_as_destination", "os", (14,)),
    # 15 cancellation
    ("test_copy_engine.py::test_cancel_*", "image", (15,)),
    ("test_reliability.py::test_cancel_*", "image", (15,)),
    ("test_copy_dialog.py::test_progress_and_cancel", "mock", (15, 18)),
    ("test_copy_dialog.py::test_shutdown_cancels_running_copy", "mock", (15, 19)),
    ("test_copy_dialog.py::test_main_window_asks_before_exit_during_copy", "mock", (15,)),
    # 16 integrity
    ("test_copy_engine.py::test_every_copy_matches_source_hash", "image", (16,)),
    ("test_copy_engine.py::test_verification_failure_discards_copy", "image", (16,)),
    ("test_copy_engine.py::test_report_text_lists_hashes", "image", (16,)),
    ("test_copy_dialog.py::test_save_report", "image", (16,)),
    ("test_real_apfs_images.py::test_content_matches_raw_on_disk_extent", "image", (16,)),
    ("test_real_apfs_images.py::test_read_file_contents", "image", (16,)),
    # 17 performance, 18 responsiveness
    ("test_performance.py::test_real_image_listing_cost_per_entry", "image", (17,)),
    ("test_performance.py::test_file_list_model_with_50000_entries", "mock", (17,)),
    ("test_performance.py::test_copy_plan_for_large_tree", "mock", (17,)),
    ("test_performance.py::test_many_small_files_copy_rate", "os", (17,)),
    ("test_performance.py::test_sorting_keeps_selection_and_folders_first", "mock", (17, 18)),
    ("test_explorer.py::test_large_folder_model_is_fast", "mock", (17,)),
    ("test_performance.py::test_window_responsive_while_large_folder_loads", "mock", (17, 18)),
    ("test_performance.py::test_window_responsive_during_copy", "mock", (18,)),
    ("test_reliability.py::test_many_small_files_do_not_flood_progress", "mock", (18,)),
    ("test_copy_dialog.py::test_one_copy_at_a_time", "image", (18,)),
    # 19 cleanup
    ("test_resources.py::test_source_image_handle_released_on_close", "os", (19,)),
    ("test_resources.py::test_source_handle_released_after_failed_open", "os", (19,)),
    ("test_resources.py::test_copied_files_are_closed", "os", (19,)),
    ("test_resources.py::test_no_handle_leak_over_repeated_sessions", "os", (19,)),
    ("test_resources.py::test_failed_and_cancelled_copies_leave_no_temp_files", "image", (19,)),
    ("test_resources.py::test_gui_copy_and_explorer_threads_end", "image", (19,)),
    ("test_real_apfs_images.py::test_close_invalidates_streams_and_reopen_works", "image", (19,)),
    ("test_backends.py::test_context_manager_closes_backend", "mock", (19,)),
    # Physical SSD through the real GUI (opt-in, admin): counted in the Physical SSD column.
    ("test_live_ssd.py::*", "hardware", (1, 2, 3, 4, 5, 7, 16, 20)),
]

# Logging privacy is an extra Phase 6 requirement, reported separately.
LOGGING_TESTS = "test_logging_privacy.py::*"
