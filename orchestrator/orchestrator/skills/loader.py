import inspect
import importlib
import importlib.util

from pathlib import Path


DEFAULT_PACKAGES = ('g1_skills',)


class SkillLibrary:
    """Skill packages: Python packages with one folder per skill, each holding contract.yaml and, when the skill
    needs code, skill.py (a Skill), agent.py (an AgentSkill), spec.py and prompt.py (read by the agent bridge too)"""

    def __init__(self, packages: list[str] | tuple[str, ...] = DEFAULT_PACKAGES):
        self.packages = list(packages)

    def directories(self) -> list[Path]:
        """Each package's folder, where its skill folders are"""
        found = []

        for package in self.packages:
            spec = importlib.util.find_spec(package)

            if spec is None or not spec.submodule_search_locations:
                raise ModuleNotFoundError(f'no skill package {package}')

            found.append(Path(next(iter(spec.submodule_search_locations))))

        return found

    def module(self, skill: str, part: str):
        """The module part (skill, agent, spec, prompt) of a skill's folder, or None when the folder has none"""
        for package, directory in zip(self.packages, self.directories()):
            if (directory / skill / f'{part}.py').exists():
                return importlib.import_module(f'{package}.{skill}.{part}')

        return None

    def find(self, skill: str, part: str, base: type):
        """The subclass of base that the skill's module part defines, or None"""
        module = self.module(skill, part)

        if module is None:
            return None

        classes = [value for value in vars(module).values()
                   if inspect.isclass(value) and issubclass(value, base) and value is not base and value.__module__ == module.__name__]

        if len(classes) != 1:
            raise TypeError(f'{module.__name__} must define one {base.__name__} subclass, not {len(classes)}')

        return classes[0]
