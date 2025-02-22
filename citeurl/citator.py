# python standard imports
import re
from copy import copy
from pathlib import Path

# third-party imports
from yaml import safe_load, safe_dump
# from appdirs import AppDirs        conditionally loaded later on

# internal imports
from .tokens import TokenType, StringBuilder
from .citation import Citation
from .authority import Authority, list_authorities
from .regex_mods import process_pattern, match_regexes

_DEFAULT_CITATOR = None

class Template:
    """
    A pattern to recognize a single kind of citation and extract
    information from it.
    """
    def __init__(
        self,
        name: str,
        tokens: dict[str, TokenType] = {},
        meta: dict[str, str] = {},
        patterns: list[str] = [],
        broad_patterns: list[str] = [],
        shortform_patterns: list[str] = [],
        idform_patterns: list[str] = [],
        name_builder: StringBuilder = None,
        URL_builder: StringBuilder = None,
        inherit_template = None,
    ):
        """
        Arguments:
            name: the name of this template
            
            tokens: The full dictionary of TokenTypes that citations from
                this template can contain. These must be listed in order
                from least-specific to most. For instance, the U.S.
                Constitution's template puts 'article' before 'section'
                before 'clause', because articles contain sections, and
                sections contain clauses.
            
            patterns: Patterns are essentially regexes to recognize
                recognize long-form citations to this template. However,
                wherever a token would appear in the regex, it should be
                replaced by the name of the token, enclosed in curly
                braces.
                
                Patterns are matched in the order that they are listed,
                so if there is a pattern that can only find a subset of
                tokens, it should be listed after the more-complete
                pattern so that the better match won't be precluded.
            
            broad_patterns: Same as `patterns`, except that they will
                only be used in contexts like search engines, where
                convenience is more important than avoiding false
                positive matches. When used, they will be used in
                addition to the normal patterns.
            
            shortform_patterns: Same as `patterns`, but these will only
                go into effect after a longform citation has been
                recognized. If a shortform pattern includes "same
                TOKEN_NAME" in curly braces, e.g. "{same volume}", the
                bracketed portion will be replaced with the exact text
                of the corresponding `raw_token` from the long-form
                citation.
            
            idform_patterns: Same as `shortform_patterns`, except that
                they will only be used to scan text until the next
                different citation occurs.
            
            URL_builder: `StringBuilder` to construct URLs for found
                citations
            
            name_builder: `StringBuilder` to construct canonical names
                of found citations
            
            meta: Optional metadata relating to this template. Patterns
                and StringBuilders can access metadata fields as if they
                were tokens, though fields can be overridden by tokens
                with the same name.
            
            inherit_template: another `Template` whose values this one
                should copy unless expressly overwritten.
        """
        kwargs = locals()
        for attr, default in {
            'name':               None,
            'tokens':             {},
            'patterns':           [],
            'broad_patterns':     [],
            'shortform_patterns': [],
            'idform_patterns':    [],
            'URL_builder':        None,
            'name_builder':       None,
            'meta':               {},
        }.items():
            if inherit_template and kwargs[attr] == default:
                value = inherit_template.__dict__.get(attr)
            elif attr.endswith('patterns') and not kwargs[attr]:
                value = []
            else:
                value = kwargs[attr]
            self.__dict__[attr] = value
        
        # update inherited StringBuilders with the correct metadata
        if inherit_template and self.meta:
            if self.URL_builder:
                self.URL_builder = copy(self.URL_builder)
                self.URL_builder.defaults = self.meta
            if self.name_builder:
                self.name_builder = copy(self.name_builder)
                self.name_builder.defaults = self.meta
        
        # use the template's metadata and tokens to make a dictionary
        # of replacements to insert into the regexes before compilation
        replacements = {k:str(v) for (k, v) in self.meta.items()}
        replacements.update({
            k:fr'(?P<{k}>{v.regex})(?!\w)'
            for (k,v) in self.tokens.items()
        })
        
        # compile the template's regexes and broad_regexes
        self.regexes = []
        self.broad_regexes = []
        for kind in ['regexes', 'broad_regexes']:
            if kind == 'broad_regexes':
                pattern_list = self.patterns + self.broad_patterns
                flags = re.I
            else:
                pattern_list = self.patterns
                flags = 0
            
            for p in pattern_list:
                pattern = process_pattern(
                    p,
                    replacements,
                    add_word_breaks=True
                )
                try:
                    regex = re.compile(pattern, flags)
                    self.__dict__[kind].append(regex)
                except re.error as e:
                    i = 'broad ' if kind == 'broad_regexes' else ''
                    raise re.error(
                        f'{self} template\'s {i}pattern "{pattern}" has '
                        f'an error: {e}'
                    )
        
        self._processed_shortforms = [
            process_pattern(p, replacements, add_word_breaks=True)
            for p in self.shortform_patterns
        ]
        self._processed_idforms = [
            process_pattern(p, replacements, add_word_breaks=True)
            for p in self.idform_patterns
        ]
    
    @classmethod
    def from_dict(cls, name: str, values: dict, inheritables: dict={}):
        """
        Return a template from a dictionary of values, like a dictionary
        created by parsing a template from YAML format.
        """
        values = {
            k.replace(' ', '_'):v
            for k,v in values.items()
        }
        
        # when pattern is listed in singular form,
        # replace it with a one-item list
        items = values.items()
        values = {}
        for key, value in items:
            if key.endswith('pattern'):
                values[key + 's'] = [value]
            else:
                values[key] = value
        
        # unrelated: when a single pattern is split
        # into a list (likely to take advantage of
        # YAML anchors), join it into one string
        for k,v in values.items():
            if not k.endswith('patterns'):
                continue
            elif v is None:
                values[k] = None
                continue
            for i, pattern in enumerate(v):
                if type(pattern) is list:
                    values[k][i] = ''.join(pattern)
        
        inherit = values.get('inherit')
        
        if inherit:
            values.pop('inherit')
            try:
                values['inherit_template'] = inheritables[inherit]
            except KeyError:
                raise KeyError(
                    f'The {name} template tried to reference template '
                    f'"{inherit}" but could not find it. Note that '
                    f'templates can only reference others that are '
                    f'defined higher up in the list, not lower.'
                )
        
        for key in ['name_builder', 'URL_builder']:
            data = values.get(key)
            if data:
                data['defaults'] = values.get('meta') or {}
                values[key] = StringBuilder.from_dict(data)
        values['tokens'] = {
            k: TokenType.from_dict(k, v)
            for k,v in values.get('tokens', {}).items()
        }
        return cls(name=name, **values)
    
    def to_dict(self) -> dict:
        "save this Template to a dictionary of values"
        output = {}
        if self.meta:
            output['meta'] = self.meta
        output['tokens'] = {
            k:v.to_dict() for k, v in self.tokens.items()
        }
        for key in ['patterns', 'shortform_patterns', 'idform_patterns']:
            value = self.__dict__.get(key)
            if not value:
                continue
            elif len(value) > 1:
                output[key] = value
            else: # de-pluralize lists that contain only one pattern
                output[key[:-1]] = value[0]
        for key in ['name_builder', 'URL_builder']:
            if self.__dict__.get(key):
                output[key] = self.__dict__[key].to_dict()
        
        spaced_output = {k.replace('_', ' '):v for k, v in output.items()}
        
        return spaced_output
    
    def to_yaml(self) -> str:
        "save this Template to a YAML string"
        return safe_dump(
            {self.name: self.to_dict()},
            sort_keys = False,
            allow_unicode = True,
        )
    
    def cite(self, text, broad: bool=True, span: tuple=(0,)) -> Citation:
        """
        Return the first citation that matches this template. If 'broad'
        is True, case-insensitive matching and broad regex patterns will
        be used. If no matches are found, return None.
        """
        regexes = self.broad_regexes if broad else self.regexes
        matches = match_regexes(text, regexes, span=span)
        for match in matches:
            try:
                return Citation(match, self)
            except SyntaxError: # invalid citation
                continue
        else:
            return None
    
    def list_longform_cites(self, text, broad: bool=False, span: tuple=(0,)):
        """
        Get a list of all long-form citations to this template found in
        the given text.
        """
        cites = []
        regexes = self.broad_regexes if broad else self.regexes
        for match in match_regexes(text, regexes, span=span):
            try:
                cites.append(Citation(match, self))
            except SyntaxError:
                continue
        return cites
    
    def __str__(self):
        return self.name
        
    def __repr__(self):
        return (
            f'Template(name="{self.name}"'
            + (f', tokens={self.tokens}' if self.tokens else '')
            + (f', meta={self.meta}' if self.meta else '')
            + (f', patterns={self.patterns}' if self.patterns else '')
            + (
                f', broad_patterns={self.broad_patterns}' 
                if self.broad_patterns else ''
            )
            + (
                f', shortform_patterns={self.shortform_patterns}'
                if self.shortform_patterns else ''
            )
            + (
                f', idform_patterns={self.idform_patterns}'
                if self.idform_patterns else ''
            )
            + (
                f', name_builder={self.name_builder}'
                if self.name_builder else ''
            )
            + (
                f', URL_builder={self.URL_builder}'
                if self.URL_builder else ''
            )
            + ')'
        )
    
    def __contains__(self, citation: Citation):
        return citation.template.name == self.name
    
    def __eq__(self, other_template):
        return repr(self) == repr(other_template)



class Citator:
    """
    A collection of citation templates, and the tools to match text
    against them en masse.
    
    Attributes:
        templates: a dictionary of citation templates that this citator
            will try to match against
    """
    
    def __init__(
        self,
        defaults = [
            'caselaw',
            'general federal law',
            'specific federal laws',
            'state law',
            'secondary sources',
        ],
        yaml_paths: list[str] = [],
        templates: dict[str, Template] = {},
    ):
        """
        Create a citator from any combination of CiteURL's default
        template sets (by default, all of them), plus any custom
        templates you want, either by pointing to custom YAML files or
        making Template objects at runtime.
        
        Arguments:
            defaults: names of files to load from the citeurl/templates
                folder. Each file contains one or more of CiteURL's
                built-in templates relevant to the given topic.
            yaml_paths: paths to custom YAML files to load templates
                from. These are loaded after the defaults, so they can
                inherit and/or overwrite them. If 
            templates: optional list of Template objects to load
                directly. These are loaded last, after the defaults and
                any yaml_paths.
        """
        self.templates = {}
        
        yamls_path = Path(__file__).parent.absolute() / 'templates'    
        for name in defaults or []:
            yaml_file = yamls_path / f'{name}.yaml'
            self.load_yaml(yaml_file.read_text())
        
        for path in yaml_paths:
            self.load_yaml(Path(path).read_text())
        self.templates.update(templates)
    
    @classmethod
    def from_yaml(cls, yaml: str):
        """
        Create a citator from scratch (i.e. without the default
        templates) by loading templates from the specified YAML string.
        """
        citator = cls(defaults=None)
        citator.load_yaml(yaml)
        return citator
    
    def to_yaml(self):
        "Save this citator to a YAML string to load later"
        yamls = [t.to_yaml() for t in self.templates.values()]
        return '\n\n'.join(yamls)
    
    def load_yaml(self, yaml: str):
        """
        Load templates from the given YAML, overwriting any existing
        templates with the same name.
        """
        for name, data in safe_load(yaml).items():
            self.templates[name] = Template.from_dict(
                name, data, inheritables=self.templates
            )
    
    def cite(self, text: str, broad: bool=True) -> Citation:
        """
        Check the given text against each of the citator's templates and
        return the first citation detected, or None.
        
        If broad is true, matching is case-insensitive and each
        template's broad regexes are used in addition to its normal
        regexes.
        """
        for template in self.templates.values():
            cite = template.cite(text, broad=broad)
            if cite:
                return cite
        else:
            return None
    
    def list_cites(
        self,
        text: str,
        id_breaks: re.Pattern = None,
    ) -> list[Citation]:
        """
        Find all citations in the given text, whether longform,
        shortform, or idform. They will be listed in order of
        appearance. If any two citations overlap, the shorter one will
        be deleted. 
        
        Wherever the id_breaks pattern appears, it will interrupt chains
        of id-form citations. This is helpful for handling unrecognized
        citations that would otherwise cause CiteURL's notion of "id."
        to get out of sync with what the text is talking about.
        """
        # first get a list of all long and shortform (not id.) citations
        longforms = []
        for template in self.templates.values():
            longforms += template.list_longform_cites(text)

        shortforms = []
        for citation in longforms:
            shortforms += citation.get_shortform_cites()

        citations = longforms + shortforms
        _sort_and_remove_overlaps(citations)
        
        # Figure out where to interrupt chains of idform citations,
        # i.e. anywhere a longform or shortform citation starts, plus
        # the start of any substring that matches the id_breaks pattern
        breakpoints = [c.span[0] for c in citations]
        if id_breaks:
            breakpoints += [
                match.span()[0] for match in
                id_breaks.finditer(text)
            ]
        breakpoints = sorted(set(breakpoints))
        
        # for each cite, look for idform citations until the next cite
        # or until the next breakpoint
        idforms = []
        for cite in citations:
            # find the next relevant breakpoint, and delete any
            # breakpoints that are already behind the current citation
            for i, breakpoint in enumerate(breakpoints):
                if breakpoint >= cite.span[1]:
                    breakpoints = breakpoints[i:]
                    break
            try:
                breakpoint = breakpoints[0]
            except IndexError:
                breakpoint = None
            
            # find the first idform reference to the citation, then the
            # first idform reference to that idform, and so on, until
            # the breakpoint
            idform = cite.get_idform_cite(until_index=breakpoint)
            while idform:
                idforms.append(idform)
                idform = idform.get_idform_cite(until_index=breakpoint)
        
        citations += idforms
        _sort_and_remove_overlaps(citations)
        return citations
    
    def list_authorities(
        self,
        text: str,
        ignored_tokens = ['subsection', 'clause', 'pincite', 'paragraph'],
        known_authorities: list = [],
        sort_by_cites: bool = True,
        id_breaks: re.Pattern = None,
    ) -> list[Authority]:
        """
        Find each distinct authority mentioned in the given text, and 
        return Authority objects whose `citations` attribute lists the
        references to each.
        
        Arguments:
            text: The string to be scanned for citations
            ignored_tokens: the names of tokens whose values are
                irrelevant to whether the citation matches an authority,
                because they  just designate portions within a single
                authority
            sort_by_cites: Whether to sort the resulting list of
                authorities by the number of citations to each one
        """
        cites = self.list_cites(text, id_breaks=id_breaks)
        return list_authorities(
            cites,
            ignored_tokens = ignored_tokens,
            known_authorities = known_authorities,
            sort_by_cites = sort_by_cites,
        )        
    
    def insert_links(
        self,
        text: str,
        attrs: dict = {'class': 'citation'},
        add_title: bool = True,
        URL_optional: bool = False,
        redundant_links: bool = True,
        id_breaks: re.Pattern = None,
        ignore_markup: bool = True,
    ) -> str:
        """
        Scan a text for citations, and return a text with each citation
        converted to a hyperlink.
        
        Arguments:
            text: the string to scan for citations.
            attrs: various HTML link attributes to give to each link
            add_title: whether to use citation.name for link titles
            URL_optional: whether to insert a hyperlink even when the
                citation does not have an associated URL
            redundant_links: whether to insert a hyperlink if it would
                point to the same URL as the previous link
            id_breaks: wherever this regex appears, interrupt chains of
                "Id."-type citations.
            ignore_markup: whether to preprocess and postprocess the
                text so that CiteURL can detect citations even when
                they contain inline markup, like "<i>Id.</i> at 32"
        
        Returns:
            text, with an HTML `a` element for each citation. 
        """
        
        # pull out all the inline HTML tags, e.g. <b>,
        # so they don't interfere with citation matching
        if ignore_markup:
            text, stored_tags = _strip_inline_tags(text)
        
        cite_offsets = []
        running_offset = 0
        
        last_URL = None
        for cite in self.list_cites(text, id_breaks = id_breaks):
            attrs['href'] = cite.URL
            if not cite.URL and not URL_optional:
                continue
            if not redundant_links and cite.URL == last_URL:
                continue
            if add_title:
                attrs['title'] = cite.name
            
            attr_str = ''.join([
                f' {k}="{v}"'
                for k, v in attrs.items() if v
            ])
            link = f'<a{attr_str}>{cite.text}</a>'
            
            cite_offset = len(link) - len(cite.text)
                    
            cite_offsets.append((
                cite.span[0], # beginning of citation
                cite_offset,
                cite.text,
            ))
            
            span = (
                cite.span[0] + running_offset,
                cite.span[1] + running_offset
            )
            
            text = text[:span[0]] + link + text[span[1]:]
            
            running_offset += cite_offset
            last_URL = cite.URL
        
        if ignore_markup:
            running_offset = 0
            for tag in stored_tags:
                temp_offset = 0
                while len(cite_offsets) > 0:
                    # only offset by a cite if the tag
                    # is after the cite start
                    if tag[1] >= cite_offsets[0][0]:
                        offset = cite_offsets[0]
                        # check if the tag is after the cite end
                        tag_start = tag[1]
                        cite_end = offset[0] + len(offset[2])
                        
                        if tag_start >= cite_end:
                            running_offset += offset[1]
                            cite_offsets.pop(0)
                        # otherwise, don't offset by the length of
                        # the cite's closing </a> tag (length of 4)
                        else:
                            temp_offset = offset[1] - 4
                            break
                    else:
                        break
                tag_pos = tag[1] + running_offset + temp_offset
                
                text = text[:tag_pos] + tag[0] + text[tag_pos:]
                
                running_offset += tag[2]
        
        return text
    
    def __iter__(self):
        return self.templates.values().__iter__()
    
    def __getitem__(self, key):
        return self.templates[key]
    
    def __setitem__(self, key, value):
        self.templates[key] = value
    
    def __eq__(self, other_citator):
        return self.templates == other_citator.templates

########################################################################
# PUBLIC FUNCTIONS
########################################################################

def cite(
    text: str,
    broad: bool = True,
    citator: Citator = None,
) -> Citation:
    """
    Convenience function to find a single citation in text, or None. See
    Citator.cite() for more info.
    """
    citator = citator or _get_default_citator()
    return citator.cite(text, broad=broad)

def list_cites(text, citator: Citator = None, id_breaks=None):
    """
    Convenience function to list all citations in a text. For more info,
    see Citator.list_cites().
    """
    citator = citator or _get_default_citator()
    return citator.list_cites(text, id_breaks=id_breaks)

def insert_links(
    text: str,
    attrs: dict = {'class': 'citation'},
    add_title: bool = True,
    URL_optional: bool = False,
    redundant_links: bool = True,
    id_breaks: re.Pattern = None,
    ignore_markup: bool = True,
    citator: Citator = None,
):
    """
    Convenience function to hyperlink all citations in a text. For more
    info, see Citator.insert_links().
    """
    citator = citator or _get_default_citator()
    return citator.insert_links(
        text = text,
        attrs = attrs,
        add_title = add_title,
        redundant_links = redundant_links,
        id_breaks = id_breaks,
        ignore_markup = ignore_markup,
    )

########################################################################
# INTERNAL FUNCTIONS
########################################################################

def _sort_and_remove_overlaps(citations: list[Citation]):
    """
    For a given list of citations found in the same text, sort them by
    their order of appearance. When two citations overlap, the shorter
    one will be deleted. The list is modified in place.
    """
    citations.sort(key=lambda x: x.span[0])
    i = 1
    while i < len(citations):
        if citations[i].span[0] < citations[i-1].span[1]:
            if len(citations[i-1]) > len(citations[i]):
                citations.pop(i)
            else:
                citations.pop(i-1)
        else:
            i += 1

def _get_default_citator():
    """
    Instantiate a citator if needed, and reuse it otherwise. If appdirs
    is installed, load default templates from your config directory
    """
    global _DEFAULT_CITATOR
    if _DEFAULT_CITATOR:
        return _DEFAULT_CITATOR
    # load custom templates from config dir, if possible
    try:
        from appdirs import AppDirs
        _appdirs = AppDirs('citeurl', 'raindrum')
        _user_config_dir = Path(_appdirs.user_config_dir)
        user_templates = set([
            file for file in _user_config_dir.iterdir()
            if file.suffix.lower() in ['.yaml', '.yml']
        ])
    except ImportError:
        user_templates = {}
    _DEFAULT_CITATOR = Citator(yaml_paths=user_templates)
    return _DEFAULT_CITATOR

def _strip_inline_tags(text: str) -> tuple[str, list[tuple]]:
    inline_tag_regex = '|'.join([
        'a', 'abbr', 'acronym', 'b', 'bdo', 'big', 'br', 'button', 'cite',
        'code', 'dfn', 'em', 'i', 'img', 'input', 'kbd', 'label', 'map',
        'object', 'output', 'q', 'samp', 'script', 'select', 'small', 'span',
        'strong', 'sub', 'sup', 'textarea', 'time', 'tt', 'var', 
    ])
    stored_tags = []
    offset = 0
    inline_tag_regex = f'</?({inline_tag_regex})(>| .+?>)'
    def store_tag(match):
        nonlocal offset
        tag_text = match.group(0)
        tag_length = len(tag_text)
        tag_start = match.span()[0] - offset
        stored_tags.append((
            tag_text,
            tag_start,
            tag_length,
        ))
        offset += tag_length
        return ''
    text = re.sub(inline_tag_regex, store_tag, text)
    return text, stored_tags
