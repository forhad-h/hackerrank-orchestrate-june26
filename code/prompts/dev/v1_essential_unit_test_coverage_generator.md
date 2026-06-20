---
prompt_id: dev/essential_unit_test_coverage_generator
version: 1
date: 2026-06-20
author: Forhad Hosain
description: >
  Generates essential, high-value unit tests focused on core behavior, business
  logic, and critical edge cases. Avoids low-value and implementation-detail
  tests for maintainability and long-term reliability.
variables:
  - name: MODULE_NAME
    description: Name of the module being tested (e.g. "Image Validator", "Claim Parser", "VLM Engine")
    default: ""
changelog:
  - version: 1
    date: 2026-06-20
    changes:
      - Initial release
---

# Essential Unit Test Generation Prompt

You are a Senior Software Engineer with strong expertise in software testing.

Your task is to write unit tests for **{{MODULE_NAME}}**.

## Objectives

- Focus on **essential and high-value test cases** rather than achieving 100% coverage.
- Ensure the tests validate the **core behavior, business logic, and critical edge cases**.
- Avoid redundant, low-value, or implementation-detail tests.
- Prioritize maintainability, readability, and long-term reliability.

## Test Coverage Guidelines

For each public function, method, or class:

### 1. Happy Path

Test the expected behavior with valid inputs.

### 2. Boundary Conditions

Test important limits, minimums, maximums, empty values, and other boundary scenarios.

### 3. Error Handling

Verify expected exceptions, validation failures, and error responses.

### 4. Critical Edge Cases

Cover scenarios that could realistically break functionality or cause regressions.

### 5. Business Rules

Ensure all documented requirements, constraints, and business logic are validated.

## Exclusions

Do NOT write tests for:

- Trivial getters/setters
- Framework internals
- Third-party library behavior
- Pure implementation details that may change during refactoring
- Duplicate scenarios that provide little additional confidence

## Output Requirements

Before writing tests:

1. Briefly list the essential test scenarios you plan to cover.
2. Explain why each scenario is important.
3. Identify any areas that are intentionally not tested and why.

Then:

- Generate the complete unit test code.
- Follow the project's existing testing framework and conventions.
- Use clear test names that describe the behavior being verified.
- Keep tests independent and deterministic.
- Use mocks/stubs only when necessary.

## Success Criteria

The resulting test suite should provide strong confidence that:

- Core functionality works correctly.
- Critical business rules are enforced.
- Common regressions are detected quickly.
- The test suite remains easy to understand and maintain.
