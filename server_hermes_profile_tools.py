"""MCP adapters for profile-scoped Hermes administration tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import operator_config as op_config
import operator_cron as op_cron
import operator_skills as op_skills


class HermesProfileTools:
    """Keep profile-scoped MCP wrappers out of the main server module."""

    def __init__(self, get_hermes_root: Callable[[], Any]):
        # Resolve the root for each call so profile-scoped environment settings
        # are read using the same runtime configuration as the rest of server.py.
        self.get_hermes_root = get_hermes_root

    def hermes_cron_list(self, profile: str = "default", include_disabled: bool = False) -> str:
        return op_cron.hermes_cron_list(
            profile=profile,
            include_disabled=include_disabled,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_cron_status(self, profile: str = "default") -> str:
        return op_cron.hermes_cron_status(
            profile=profile, hermes_root=self.get_hermes_root()
        )

    def hermes_cron_run(
        self,
        profile: str = "default",
        job_id: str = "",
        dry_run: bool = True,
        timeout: int = 1800,
    ) -> str:
        return op_cron.hermes_cron_run(
            profile=profile,
            job_id=job_id,
            dry_run=dry_run,
            timeout=timeout,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_cron_pause(
        self,
        profile: str = "default",
        job_id: str = "",
        reason: str = "",
        dry_run: bool = True,
    ) -> str:
        return op_cron.hermes_cron_pause(
            profile=profile,
            job_id=job_id,
            reason=reason,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_cron_copy(
        self,
        source_profile: str,
        target_profile: str,
        job_id: str,
        dry_run: bool = True,
    ) -> str:
        return op_cron.hermes_cron_copy(
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=job_id,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_cron_create(
        self,
        profile: str = "default",
        schedule: str = "",
        prompt: str = "",
        name: str | None = None,
        skills: list[str] | None = None,
        deliver: str | None = None,
        repeat: int | None = None,
        script: str | None = None,
        workdir: str | None = None,
        no_agent: bool | None = None,
        context_from: list[str] | None = None,
        enabled_toolsets: list[str] | None = None,
        model_provider: str | None = None,
        model_name: str | None = None,
        dry_run: bool = True,
    ) -> str:
        return op_cron.hermes_cron_create(
            profile=profile,
            schedule=schedule,
            prompt=prompt,
            name=name,
            skills=skills,
            deliver=deliver,
            repeat=repeat,
            script=script,
            workdir=workdir,
            no_agent=no_agent,
            context_from=context_from,
            enabled_toolsets=enabled_toolsets,
            model_provider=model_provider,
            model_name=model_name,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_cron_move(
        self,
        source_profile: str,
        target_profile: str,
        job_id: str,
        pause_source: bool = True,
        test_run_target: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_cron.hermes_cron_move(
            source_profile=source_profile,
            target_profile=target_profile,
            job_id=job_id,
            pause_source=pause_source,
            test_run_target=test_run_target,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_skill_diff(
        self,
        profile: str = "default",
        name: str = "",
        proposed_content: str | None = None,
        old_string: str | None = None,
        new_string: str | None = None,
        file_path: str = "SKILL.md",
    ) -> str:
        return op_skills.hermes_skill_diff(
            profile=profile,
            name=name,
            proposed_content=proposed_content,
            old_string=old_string,
            new_string=new_string,
            file_path=file_path,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_skill_create(
        self,
        profile: str = "default",
        name: str = "",
        content: str = "",
        dry_run: bool = True,
    ) -> str:
        return op_skills.hermes_skill_create(
            profile=profile,
            name=name,
            content=content,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_skill_edit(
        self,
        profile: str = "default",
        name: str = "",
        content: str = "",
        dry_run: bool = True,
    ) -> str:
        return op_skills.hermes_skill_edit(
            profile=profile,
            name=name,
            content=content,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_skill_patch(
        self,
        profile: str = "default",
        name: str = "",
        old_string: str = "",
        new_string: str = "",
        file_path: str = "SKILL.md",
        replace_all: bool = False,
        dry_run: bool = True,
    ) -> str:
        return op_skills.hermes_skill_patch(
            profile=profile,
            name=name,
            old_string=old_string,
            new_string=new_string,
            file_path=file_path,
            replace_all=replace_all,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_skill_write_file(
        self,
        profile: str = "default",
        name: str = "",
        file_path: str = "",
        file_content: str = "",
        dry_run: bool = True,
    ) -> str:
        return op_skills.hermes_skill_write_file(
            profile=profile,
            name=name,
            file_path=file_path,
            file_content=file_content,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_skill_copy(
        self,
        source_profile: str,
        target_profile: str,
        name: str,
        dry_run: bool = True,
    ) -> str:
        return op_skills.hermes_skill_copy(
            source_profile=source_profile,
            target_profile=target_profile,
            name=name,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_skill_sync_to_default(
        self, source_profile: str, name: str, dry_run: bool = True
    ) -> str:
        return op_skills.hermes_skill_sync_to_default(
            source_profile=source_profile,
            name=name,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_skill_delete(
        self,
        profile: str = "default",
        name: str = "",
        dry_run: bool = True,
    ) -> str:
        return op_skills.hermes_skill_delete(
            profile=profile,
            name=name,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_config_get(
        self, profile: str = "default", key_path: str | None = None
    ) -> str:
        return op_config.hermes_config_get(
            profile=profile,
            key_path=key_path,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_config_set(
        self,
        profile: str = "default",
        key_path: str = "",
        value: Any = None,
        dry_run: bool = True,
    ) -> str:
        return op_config.hermes_config_set(
            profile=profile,
            key_path=key_path,
            value=value,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_config_patch(
        self,
        profile: str = "default",
        old_string: str = "",
        new_string: str = "",
        dry_run: bool = True,
    ) -> str:
        return op_config.hermes_config_patch(
            profile=profile,
            old_string=old_string,
            new_string=new_string,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_env_status(
        self, profile: str = "default", keys: list[str] | None = None
    ) -> str:
        return op_config.hermes_env_status(
            profile=profile,
            keys=keys,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_env_set_nonsecret(
        self,
        profile: str = "default",
        key: str = "",
        value: str = "",
        dry_run: bool = True,
    ) -> str:
        return op_config.hermes_env_set_nonsecret(
            profile=profile,
            key=key,
            value=value,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )

    def hermes_env_copy_nonsecret(
        self,
        source_profile: str,
        target_profile: str,
        key: str,
        dry_run: bool = True,
    ) -> str:
        return op_config.hermes_env_copy_nonsecret(
            source_profile=source_profile,
            target_profile=target_profile,
            key=key,
            dry_run=dry_run,
            hermes_root=self.get_hermes_root(),
        )
