"""Backfill Magic Wand (AI bio extraction) over contacts in bulk.

Targets contacts that have a twitter or telegram bio but are missing
`title` or `company` in their Contact Details. For each, calls the
same `extract_from_bios()` service that powers the per-contact Magic
Wand button, then applies the same write rules as the endpoint:

  - Fills empty Contact fields (title, company, given/family/full_name).
  - Auto-creates / enriches the linked Organization (website, industry,
    location, logo).
  - Idempotent — only writes empty fields (given/family_name overwrite).

Usage:
    # preview counts + a sample, no writes, no LLM calls
    python -m scripts.backfill_bio_extraction --dry-run

    # full run with default concurrency of 5
    python -m scripts.backfill_bio_extraction

    # cap to first N eligible contacts (useful for staged rollout)
    python -m scripts.backfill_bio_extraction --limit 50

    # tune Anthropic concurrency
    python -m scripts.backfill_bio_extraction --concurrency 10
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("backfill_bio_extraction")


async def _eligible_contact_ids(db: AsyncSession, limit: int | None) -> list[uuid.UUID]:
    """Contacts with twitter or telegram bio AND missing title or company."""
    # Defer import so script can import without pulling FastAPI app at parse-time.
    from app.models.contact import Contact

    has_twitter_bio = func.length(func.trim(Contact.twitter_bio)) > 0
    has_telegram_bio = func.length(func.trim(Contact.telegram_bio)) > 0
    title_empty = or_(Contact.title.is_(None), func.length(func.trim(Contact.title)) == 0)
    company_empty = or_(Contact.company.is_(None), func.length(func.trim(Contact.company)) == 0)

    stmt = (
        select(Contact.id)
        .where(or_(has_twitter_bio, has_telegram_bio))
        .where(or_(title_empty, company_empty))
        .order_by(Contact.created_at.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).scalars().all()
    return list(rows)


async def _process_one(
    factory: async_sessionmaker[AsyncSession],
    contact_id: uuid.UUID,
    sem: asyncio.Semaphore,
) -> tuple[uuid.UUID, list[str], str | None]:
    """Run Magic Wand against a single contact. Returns (id, fields_updated, error)."""
    from app.models.contact import Contact
    from app.services.bio_extractor import extract_from_bios
    from app.services.organization_service import auto_create_organization, download_org_logo

    async with sem:
        async with factory() as db:
            try:
                contact = (
                    await db.execute(select(Contact).where(Contact.id == contact_id))
                ).scalar_one_or_none()
                if contact is None:
                    return contact_id, [], "not found"

                extracted = await extract_from_bios(
                    full_name=contact.full_name,
                    given_name=contact.given_name,
                    family_name=contact.family_name,
                    title=contact.title,
                    company=contact.company,
                    twitter_bio=contact.twitter_bio,
                    telegram_bio=contact.telegram_bio,
                    linkedin_bio=contact.linkedin_bio,
                    linkedin_headline=contact.linkedin_headline,
                )

                fields_updated: list[str] = []
                if not extracted:
                    return contact_id, fields_updated, None

                # Same write rules as POST /contacts/{id}/extract-bio
                for field in ("given_name", "family_name", "title", "company"):
                    new_val = extracted.get(field)
                    if not new_val:
                        continue
                    old_val = getattr(contact, field, None) or ""
                    if field in ("given_name", "family_name") or not old_val:
                        if new_val != old_val:
                            setattr(contact, field, new_val)
                            fields_updated.append(field)

                if "given_name" in fields_updated or "family_name" in fields_updated:
                    new_full = " ".join(
                        filter(None, [contact.given_name, contact.family_name])
                    ) or contact.full_name
                    if new_full != contact.full_name:
                        contact.full_name = new_full
                        if "full_name" not in fields_updated:
                            fields_updated.append("full_name")

                if extracted.get("company"):
                    org = await auto_create_organization(contact, contact.user_id, db)
                    if org:
                        org_updated = False
                        if extracted.get("company_website") and not org.website:
                            org.website = extracted["company_website"]
                            org_updated = True
                            fields_updated.append("company_website")
                        if extracted.get("company_industry") and not org.industry:
                            org.industry = extracted["company_industry"]
                            org_updated = True
                            fields_updated.append("company_industry")
                        if extracted.get("company_location") and not org.location:
                            org.location = extracted["company_location"]
                            org_updated = True
                            fields_updated.append("company_location")
                        if org_updated and org.website and not org.logo_url:
                            logo_url = await download_org_logo(org.website, org.id)
                            if logo_url:
                                org.logo_url = logo_url

                if fields_updated:
                    await db.commit()
                return contact_id, fields_updated, None
            except Exception as exc:
                await db.rollback()
                logger.exception("extract failed for contact %s", contact_id)
                return contact_id, [], str(exc)


async def main(*, dry_run: bool, limit: int | None, concurrency: int) -> int:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL not set", file=sys.stderr)
        return 2

    engine = create_async_engine(db_url, echo=False)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    async with factory() as db:
        ids = await _eligible_contact_ids(db, limit)

    total = len(ids)
    logger.info("eligible contacts: %d (limit=%s)", total, limit)

    if dry_run:
        sample = ids[:10]
        for cid in sample:
            logger.info("  sample id: %s", cid)
        if total > len(sample):
            logger.info("  ... and %d more", total - len(sample))
        logger.info("[dry-run] not calling LLM, not writing.")
        await engine.dispose()
        return 0

    if total == 0:
        logger.info("nothing to backfill.")
        await engine.dispose()
        return 0

    sem = asyncio.Semaphore(concurrency)
    tasks = [asyncio.create_task(_process_one(factory, cid, sem)) for cid in ids]

    done_count = 0
    updated_count = 0
    no_op_count = 0
    error_count = 0
    fields_tally: dict[str, int] = {}

    for coro in asyncio.as_completed(tasks):
        cid, fields, err = await coro
        done_count += 1
        if err:
            error_count += 1
        elif fields:
            updated_count += 1
            for f in fields:
                fields_tally[f] = fields_tally.get(f, 0) + 1
            logger.info("[%d/%d] %s -> %s", done_count, total, cid, ",".join(fields))
        else:
            no_op_count += 1
        if done_count % 25 == 0:
            logger.info(
                "progress: %d/%d (updated=%d no_op=%d errors=%d)",
                done_count, total, updated_count, no_op_count, error_count,
            )

    logger.info("done. total=%d updated=%d no_op=%d errors=%d",
                total, updated_count, no_op_count, error_count)
    for f, n in sorted(fields_tally.items(), key=lambda kv: -kv[1]):
        logger.info("  field %s: %d", f, n)

    await engine.dispose()
    return 0 if error_count == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show eligible-contact count and sample IDs; do not call LLM or write.",
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Cap to the first N eligible contacts (ordered by created_at).",
    )
    parser.add_argument(
        "--concurrency", type=int, default=5,
        help="Max in-flight Anthropic requests (default: 5).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main(
        dry_run=args.dry_run,
        limit=args.limit,
        concurrency=args.concurrency,
    )))
