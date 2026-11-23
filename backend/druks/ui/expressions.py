import ast
import inspect
import math
from typing import ClassVar

from pydantic import ValidationError

from druks.exceptions import DruksError
from druks.schemas import Schema


class Expression:
    """One kind of text that the agent writes: calls of druks.ui classes. A call names a
    class by its bare name or with the ``ui.`` prefix. It gives the arguments of the
    class's own ``__init__`` without a name, in their order, and every other argument by
    its name. Each message says what to change."""

    error: ClassVar[type[DruksError]]
    # The plural noun that names the text in each message.
    noun: ClassVar[str]
    text_limit: ClassVar[int]
    depth_limit: ClassVar[int]
    # The classes that a call inside a call can name, by name.
    models: ClassVar[dict[str, type[Schema]]]
    # The reason for each class and each argument that the text cannot name.
    refused: ClassVar[dict[str, str]]

    @classmethod
    def get_expression(cls, text: str) -> ast.expr:
        """The expression that ``text`` holds."""
        if len(text) > cls.text_limit:
            raise cls.error(
                f"The {cls.noun} are {len(text)} characters long. Write them in "
                f"{cls.text_limit} characters or fewer."
            )
        try:
            return ast.parse(text, filename=cls.noun, mode="eval").body
        except SyntaxError as error:
            raise cls.error(f"The {cls.noun} are not valid Python: {error}.") from error
        # The parser runs out of stack on a text that nests deep.
        except (RecursionError, MemoryError) as error:
            raise cls.error(f"The {cls.noun} nest too deep. Write one list of calls.") from error

    @classmethod
    def get_object(cls, node: ast.expr, models: dict[str, type[Schema]], depth: int) -> Schema:
        """The object that the call ``node`` makes: one of ``models``. Pydantic checks the
        arguments."""
        match node:
            case ast.Call(
                func=ast.Name(id=name) | ast.Attribute(value=ast.Name(id="ui"), attr=name)
            ):
                pass
            case _:
                raise cls.error("Call each druks.ui class by its bare name or with the ui. prefix.")
        if name in cls.refused:
            raise cls.error(cls.refused[name])
        if name not in models:
            raise cls.error(f"{name} is not one of {', '.join(models)}.")
        model = models[name]
        # The first parameter of __init__ is the object itself.
        positions = [
            parameter
            for parameter in list(inspect.signature(model.__init__).parameters.values())[1:]
            if parameter.kind == parameter.POSITIONAL_OR_KEYWORD
        ]
        if len(node.args) > len(positions):
            parameters = " and ".join(position.name for position in positions) or "no argument"
            raise cls.error(f"{name} takes {parameters} without a name. Give the rest by name.")
        arguments = {
            position.name: cls.get_value(argument, depth + 1)
            for position, argument in zip(positions, node.args, strict=False)
        }
        for keyword in node.keywords:
            if keyword.arg in cls.refused:
                raise cls.error(cls.refused[keyword.arg])
            if keyword.arg not in model.model_fields:
                raise cls.error(
                    f"{name} takes no argument {keyword.arg or '**'}. "
                    f"It takes {', '.join(model.model_fields)}."
                )
            if keyword.arg in arguments:
                raise cls.error(f"{name} takes {keyword.arg} once.")
            arguments[keyword.arg] = cls.get_value(keyword.value, depth + 1)
        # model_validate calls the class's own __init__. A missing argument that __init__
        # requires raises a TypeError there, not a ValidationError.
        for position in positions:
            if position.default is position.empty and position.name not in arguments:
                raise cls.error(f"{name}: {position.name}: Field required.")
        try:
            return model.model_validate(arguments)
        except ValidationError as error:
            reasons = []
            for detail in error.errors():
                place = ".".join(map(str, detail["loc"]))
                reason = detail["msg"].removesuffix(".")
                reasons.append(f"{place}: {reason}" if place else reason)
            raise cls.error(f"{name}: {'; '.join(reasons)}.") from error

    @classmethod
    def get_value(cls, node: ast.expr, depth: int):
        """The value of the argument ``node``: a literal, a signed number, a list of values,
        or a call of one of ``models``."""
        if depth > cls.depth_limit:
            raise cls.error(f"The {cls.noun} nest deeper than {cls.depth_limit} levels.")
        match node:
            case ast.Constant(value=str() | bool() | None):
                return node.value
            case (
                ast.Constant(value=int() | float() as number)
                | ast.UnaryOp(op=ast.UAdd(), operand=ast.Constant(value=int() | float() as number))
            ) if abs(number) < math.inf:
                return number
            case ast.UnaryOp(
                op=ast.USub(), operand=ast.Constant(value=int() | float() as number)
            ) if abs(number) < math.inf:
                return -number
            case ast.List():
                return [cls.get_value(element, depth + 1) for element in node.elts]
            case ast.Call():
                return cls.get_object(node, cls.models, depth)
        raise cls.error(
            f"The {cls.noun} hold only literals, lists, and calls of druks.ui classes, and "
            f"Druks runs no code. Replace the {type(node).__name__} at line {node.lineno}, "
            f"column {node.col_offset + 1}."
        )
