"""M1 verification: the business rules of docs/SHOPPING_RULES.md, asserted against the fixtures.

These are the rules the whole system leans on, so they are tested at the service layer — the one
place the three backend drivers share.
"""

from __future__ import annotations

import pytest
from sqlalchemy.engine import Engine

from shopping_backend.domain.enums import PreferenceCategory, UnavailableReason
from shopping_backend.domain.schemas import GoodsQuery, PreferenceFact
from shopping_backend.seeds.loader import load_seeds
from shopping_backend.services import Services
from shopping_backend.services.preference import find_blocked


def test_phone_search_boundary_and_off_sale(services: Services) -> None:
    """R-G3 inclusive boundary + R-G1: exactly three on-sale phones sit at or under ¥2000."""
    found = services.goods.search(GoodsQuery(category="phone", max_price_cents=200000, limit=20))
    assert {item.id for item in found} == {"g_0001", "g_0002", "g_0003"}
    assert "g_0004" not in {item.id for item in found}, "¥2000.01 must not match '2000元以内'"
    assert all(item.price_cents <= 200000 for item in found)


def test_off_sale_never_returned_unless_explicitly_allowed(services: Services) -> None:
    default = services.goods.search(GoodsQuery(query="华为 nova 12", limit=20))
    assert "g_0005" not in {item.id for item in default}

    allowed = services.goods.search(GoodsQuery(query="华为 nova 12", limit=20, include_off_sale=True))
    assert "g_0005" in {item.id for item in allowed}


def test_text_matching_prefers_title_containment(services: Services) -> None:
    found = services.goods.search(GoodsQuery(query="Redmi Note 13", limit=5))
    assert found[0].id == "g_0001"

    chinese = services.goods.search(GoodsQuery(query="耳机", limit=10))
    assert {item.id for item in chinese} >= {"g_0006", "g_0007"}
    assert all(item.category == "earphone" for item in chinese)


def test_price_filter_excludes_above_threshold(services: Services) -> None:
    found = services.goods.search(
        GoodsQuery(category="phone", min_price_cents=165000, max_price_cents=200000, limit=20)
    )
    assert {item.id for item in found} == {"g_0001", "g_0003"}


# --------------------------------------------------------------------------- stock (R-S1..R-S5)


def test_available_is_aggregated_across_warehouses(services: Services) -> None:
    stock = services.stock.detail("g_0001")
    assert stock is not None
    assert stock.on_hand == 15
    assert stock.reserved == 3
    assert stock.available == 12  # (10-2) + (5-1), not 15-3 of a merged pool
    assert len(stock.warehouses) == 2


def test_reserved_above_on_hand_clamps_to_zero(services: Services) -> None:
    stock = services.stock.detail("g_0009")
    assert stock is not None
    assert stock.available == 0


def test_tight_stock_sample_reports_two_units(services: Services) -> None:
    stock = services.stock.detail("g_0002")
    assert stock is not None and stock.available == 2


def test_goods_without_stock_rows_is_omitted_by_service(services: Services) -> None:
    """The service only reports rows that exist; R-S2 zero-filling happens in the guardrail layer."""
    found = {stock.goods_id for stock in services.stock.query(["g_0001", "g_0008"])}
    assert found == {"g_0001"}


def test_sold_out_earphone_sample(services: Services) -> None:
    stock = services.stock.detail("g_0006")
    assert stock is not None and stock.available == 0


# --------------------------------------------------------------------------- coupons (§2.3)


def test_single_best_coupon_for_budget_phone(services: Services) -> None:
    selection = services.coupon.pick(["g_0001"])
    assert selection.codes_for("g_0001") == ["M1500_100"]
    assert selection.by_goods["g_0001"][0].display == "满¥1500.00减¥100.00"  # R-C2 / O4


def test_stronger_coupon_wins_at_higher_price(services: Services) -> None:
    selection = services.coupon.pick(["g_0003"])
    assert selection.codes_for("g_0003") == ["M2000_150"], "¥150 off outranks ¥100 off once the ¥2000 threshold is met"


def test_sold_out_earphone_still_gets_a_coupon(services: Services) -> None:
    """F3/R-S4: the out-of-stock branch must still surface a usable coupon."""
    selection = services.coupon.pick(["g_0006"])
    assert selection.codes_for("g_0006") == ["M199_20"]


def test_general_coupon_query_only_returns_scope_all(services: Services) -> None:
    """R-C4: no goods context -> only scope=all, valid, in-quota coupons."""
    selection = services.coupon.pick([])
    assert [coupon.code for coupon in selection.general] == ["M5000_600", "FREESHIP"]
    assert all(coupon.scope == "all" for coupon in selection.general)


def test_rejected_coupons_carry_single_reason_codes(services: Services) -> None:
    selection = services.coupon.pick([])
    reasons = {item.code: item.reason for item in selection.unavailable}
    assert reasons["M500_50"] == UnavailableReason.PAUSED
    assert reasons["M1000_200"] == UnavailableReason.EXPIRED
    assert reasons["M300_30"] == UnavailableReason.QUOTA_USED_UP


def test_threshold_above_price_is_not_offered(services: Services) -> None:
    selection = services.coupon.pick(["g_0011"])  # ¥129 accessory
    codes = selection.codes_for("g_0011")
    assert codes and "M2000_150" not in codes


# --------------------------------------------------------------------------- preferences


def test_tier_one_puts_constraints_first(services: Services) -> None:
    """R-P2 guarantees constraints precede preferences; ordering *within* a category falls back to
    recency, which is not observable here because the fixtures were stamped in the same second."""
    facts = services.preference.tier_one("u_1001")
    ordered = list(facts)
    assert set(ordered[:2]) == {"budget_limit", "avoid_brand"}
    assert ordered[-1] == "preferred_category"
    assert facts["budget_limit"] == "预算不超过 2000 元"


def test_seed_write_filter_refused_card_number(engine: Engine, services: Services) -> None:
    report = load_seeds(engine, reset=True)
    assert ("card_hint", "long_digit_run") in report.preferences_rejected
    assert "card_hint" not in {row.pref_key for row in services.preference.load("u_1002")}
    assert report.preferences == 5  # u_1001 x3 + u_1002 x2, the card fixture excluded


def test_write_filter_blocks_and_does_not_echo(services: Services) -> None:
    assert find_blocked("卡号 6222021388001234") == "long_digit_run"
    assert find_blocked("联系我 name@example.com") == "email"
    assert find_blocked("预算 2000 元以内") is None

    result = services.preference.store(
        "u_1001", [PreferenceFact(key="card", value="6222021388001234")], source_session_id="s_x"
    )
    assert result.kept == []
    assert result.rejected and result.rejected[0][1] == "long_digit_run"
    assert "6222" not in str(result.rejected)  # the offending value is never echoed
    assert "card" not in {row.pref_key for row in services.preference.load("u_1001")}


def test_store_upserts_same_key_without_duplicates(services: Services) -> None:
    services.preference.store(
        "u_1001",
        [PreferenceFact(key="budget_limit", value="预算不超过 1500 元", category=PreferenceCategory.CONSTRAINT)],
    )
    rows = [row for row in services.preference.load("u_1001") if row.pref_key == "budget_limit"]
    assert len(rows) == 1
    assert rows[0].pref_value == "预算不超过 1500 元"


def test_similar_fact_under_new_key_is_treated_as_duplicate(services: Services) -> None:
    result = services.preference.store("u_1001", [PreferenceFact(key="budget_alias", value="预算不超过2000元")])
    assert result.kept == []
    assert "budget_alias" in result.duplicate_keys


def test_local_preferences_win_over_maxkb_summary(services: Services) -> None:
    merged = services.preference.merged("u_1001", {"budget_limit": "预算 5000 元", "from_maxkb": "hi"})
    assert merged["budget_limit"] == "预算不超过 2000 元"
    assert merged["from_maxkb"] == "hi"


def test_single_turn_write_cap(services: Services) -> None:
    facts = [PreferenceFact(key=f"k_{index}", value=f"值 {index}") for index in range(6)]
    result = services.preference.store("u_1003", facts)
    assert len(result.kept) == 3


def test_clear_removes_all(services: Services) -> None:
    assert services.preference.clear("u_1001") >= 1
    assert services.preference.load("u_1001") == []


@pytest.mark.parametrize("bad", ["6222021388001234", "DE89370400440532013000", "a@b.com", "13800138000"])
def test_blocked_patterns(bad: str) -> None:
    assert find_blocked(bad) is not None
