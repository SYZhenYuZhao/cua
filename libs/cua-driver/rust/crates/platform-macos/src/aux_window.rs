// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Cua AI, Inc.

//! Semantic recognition of AppKit's window-sharing indicator.
//!
//! Recognition rule informed by Callstack agent-device PR #3965 (MIT):
//! https://github.com/callstack/agent-device/pull/3965
//! This implementation is independently written for CUA's exact CGWindowID bindings.
//! No window-size or localized window-title heuristic is used. Unknown AX state
//! never removes a business window from consideration.

use std::{
    collections::HashSet,
    time::{Duration, Instant},
};

use core_foundation::{
    array::CFArray,
    base::{CFGetTypeID, CFRelease, CFRetain, CFTypeRef, TCFType},
    string::CFString,
};

use crate::ax::bindings::{
    ax_get_window_id, kAXErrorSuccess, AXUIElementCopyAttributeValue, AXUIElementCreateApplication,
    AXUIElementGetTypeID, AXUIElementRef, AXUIElementSetMessagingTimeout,
};

const PROBE_DEADLINE: Duration = Duration::from_millis(300);
const REPLY_TIMEOUT_SECONDS: f32 = 0.05;
const MAX_WINDOWS: isize = 128;
const MAX_DIRECT_CHILDREN: isize = 16;
const MAX_READS: usize = 512;
const MAX_NODES: usize = 256;
const SHARING_BUTTON_TITLE: &str = "WindowSharingSessionButton";

#[link(name = "ApplicationServices", kind = "framework")]
extern "C" {
    fn AXUIElementGetPid(element: AXUIElementRef, pid: *mut i32) -> i32;
}

/// A retained AX reference. Reset its element-specific timeout before release;
/// other code may resolve the same remote AX object after this bounded probe.
struct Element(AXUIElementRef);

impl Drop for Element {
    fn drop(&mut self) {
        unsafe {
            AXUIElementSetMessagingTimeout(self.0, 0.0);
            CFRelease(self.0 as CFTypeRef);
        }
    }
}

struct CopiedValue(CFTypeRef);

impl Drop for CopiedValue {
    fn drop(&mut self) {
        unsafe { CFRelease(self.0) }
    }
}

struct Budget {
    deadline: Instant,
    reads_left: usize,
    nodes_left: usize,
}

impl Budget {
    fn new() -> Self {
        Self {
            deadline: Instant::now() + PROBE_DEADLINE,
            reads_left: MAX_READS,
            nodes_left: MAX_NODES,
        }
    }

    /// Bound local array enumeration as well as the individual remote reads.
    fn take_node(&mut self) -> bool {
        if Instant::now() >= self.deadline || self.nodes_left == 0 || self.reads_left == 0 {
            return false;
        }
        self.nodes_left -= 1;
        true
    }

    /// Check before every remote request, not merely once per AX element.
    fn prepare(&mut self, element: AXUIElementRef) -> bool {
        let remaining = self.deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() || self.reads_left == 0 {
            return false;
        }
        self.reads_left -= 1;
        let timeout = remaining.as_secs_f32().min(REPLY_TIMEOUT_SECONDS);
        unsafe { AXUIElementSetMessagingTimeout(element, timeout) == kAXErrorSuccess }
    }

    fn copy_attribute(&mut self, element: AXUIElementRef, attribute: &str) -> Option<CopiedValue> {
        if !self.prepare(element) {
            return None;
        }
        let attribute = CFString::new(attribute);
        let mut value = std::ptr::null();
        let result = unsafe {
            AXUIElementCopyAttributeValue(element, attribute.as_concrete_TypeRef(), &mut value)
        };
        // Even an unexpected error plus a non-null copied value must not leak.
        let owned = (!value.is_null()).then(|| CopiedValue(value));
        (result == kAXErrorSuccess).then_some(owned).flatten()
    }

    fn string(&mut self, element: AXUIElementRef, attribute: &str) -> Option<String> {
        let value = self.copy_attribute(element, attribute)?;
        if unsafe { CFGetTypeID(value.0) } != CFString::type_id() {
            return None;
        }
        Some(unsafe { CFString::wrap_under_get_rule(value.0 as _) }.to_string())
    }

    fn array(&mut self, element: AXUIElementRef, attribute: &str) -> Option<CFArray<CFTypeRef>> {
        let value = self.copy_attribute(element, attribute)?;
        if unsafe { CFGetTypeID(value.0) } != CFArray::<CFTypeRef>::type_id() {
            return None;
        }
        Some(unsafe { CFArray::wrap_under_get_rule(value.0 as _) })
    }
}

/// Retain a borrowed array item only when it really is an AXUIElement.
fn element_at(array: &CFArray<CFTypeRef>, index: isize) -> Option<Element> {
    let value = *array.get(index)?;
    if value.is_null() || unsafe { CFGetTypeID(value) } != unsafe { AXUIElementGetTypeID() } {
        return None;
    }
    unsafe { CFRetain(value) };
    Some(Element(value as AXUIElementRef))
}

fn sharing_button(role: Option<&str>, title: impl FnOnce() -> Option<String>) -> bool {
    role == Some("AXButton") && title().as_deref() == Some(SHARING_BUTTON_TITLE)
}

fn eligible_window(owner_pid: Option<i32>, pid: i32, role: Option<&str>, id: Option<u32>) -> bool {
    owner_pid == Some(pid) && role == Some("AXWindow") && id.is_some_and(|id| id != 0)
}

/// Positive semantic matches only. An empty result can mean no indicator OR
/// inaccessible/incomplete AX data; it is never proof that all windows are ordinary.
/// This read-only probe never activates, raises or restores a window.
pub(crate) fn sharing_indicator_window_ids(pid: i32) -> HashSet<u32> {
    let mut ids = HashSet::new();
    let application = unsafe { AXUIElementCreateApplication(pid) };
    if application.is_null() {
        return ids;
    }
    let application = Element(application);
    let mut budget = Budget::new();
    let Some(windows) = budget.array(application.0, "AXWindows") else {
        return ids;
    };
    for index in 0..windows.len().min(MAX_WINDOWS) {
        if !budget.take_node() {
            break;
        }
        let Some(window) = element_at(&windows, index) else {
            continue;
        };
        if !budget.prepare(window.0) {
            break;
        }
        let mut owner = 0;
        let owner = (unsafe { AXUIElementGetPid(window.0, &mut owner) } == kAXErrorSuccess)
            .then_some(owner);
        let role = budget.string(window.0, "AXRole");
        if !budget.prepare(window.0) {
            break;
        }
        let id = unsafe { ax_get_window_id(window.0) };
        if !eligible_window(owner, pid, role.as_deref(), id) {
            continue;
        }
        let Some(children) = budget.array(window.0, "AXChildren") else {
            continue;
        };
        for child_index in 0..children.len().min(MAX_DIRECT_CHILDREN) {
            if !budget.take_node() {
                break;
            }
            let Some(child) = element_at(&children, child_index) else {
                continue;
            };
            let role = budget.string(child.0, "AXRole");
            if sharing_button(role.as_deref(), || budget.string(child.0, "AXTitle")) {
                if let Some(id) = id {
                    ids.insert(id);
                }
                break;
            }
        }
    }
    ids
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn indicator_requires_exact_window_owner_role_and_real_id() {
        assert!(eligible_window(Some(42), 42, Some("AXWindow"), Some(7)));
        for (owner, role, id) in [
            (None, Some("AXWindow"), Some(7)),
            (Some(43), Some("AXWindow"), Some(7)),
            (Some(42), None, Some(7)),
            (Some(42), Some("AXDialog"), Some(7)),
            (Some(42), Some("AXWindow"), None),
            (Some(42), Some("AXWindow"), Some(0)),
        ] {
            assert!(!eligible_window(owner, 42, role, id));
        }
    }

    #[test]
    fn only_the_semantic_button_is_a_positive_match() {
        assert!(sharing_button(Some("AXButton"), || Some(
            SHARING_BUTTON_TITLE.into()
        )));
        for title in [
            None,
            Some("Window"),
            Some("Close"),
            Some("windowsharingsessionbutton"),
        ] {
            assert!(!sharing_button(Some("AXButton"), || title.map(str::to_owned)));
        }
        for role in [
            None,
            Some("AXStaticText"),
            Some("AXGroup"),
            Some("AXWindow"),
        ] {
            assert!(!sharing_button(role, || panic!(
                "non-buttons must not read titles"
            )));
        }
    }

    #[test]
    fn array_enumeration_obeys_its_node_limit() {
        let mut budget = Budget::new();
        budget.nodes_left = 1;
        assert!(budget.take_node());
        assert!(!budget.take_node());
    }

    #[test]
    fn exhausted_budget_performs_no_ax_request() {
        let mut budget = Budget {
            deadline: Instant::now() + PROBE_DEADLINE,
            reads_left: 0,
            nodes_left: MAX_NODES,
        };
        assert!(!budget.prepare(std::ptr::null_mut()));
        assert!(!budget.take_node());
        budget.reads_left = MAX_READS;
        budget.deadline = Instant::now();
        assert!(!budget.take_node());
        assert!(!budget.prepare(std::ptr::null_mut()));
    }
}
