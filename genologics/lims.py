# coding=utf-8

"""Python interface to GenoLogics LIMS via its REST API.

LIMS interface.

Per Kraulis, Science for Life Laboratory, Stockholm, Sweden.
Copyright (C) 2012 Per Kraulis
"""

__all__ = ['Lab', 'Researcher', 'Project', 'Sample',
           'Containertype', 'Container', 'Processtype', 'Process',
           'Artifact', 'Lims', 'Step', 'Queue', 'File', 'Glsstorage',
           'ReagentLot', 'ReagentKit', 'Workflow', 'ReagentType',
           'ProtocolStep']

import os
import re
from io import BytesIO
import requests

# python 2.7, 3+ compatibility
from sys import version_info

if version_info[0] == 2:
    from urlparse import urljoin
    from urllib import urlencode
else:
    from urllib.parse import urljoin
    from urllib.parse import urlencode


from .entities import *

# Python 2.6 support work-around
if hasattr(ElementTree, 'ParseError'):
    ETREE_EXCEPTION = ElementTree.ParseError
else:
    from xml.parsers import expat
    ETREE_EXCEPTION = expat.ExpatError

TIMEOUT = 16


class Lims(object):
    "LIMS interface through which all entity instances are retrieved."

    VERSION = 'v2'

    def __init__(self, baseuri, username, password, version=VERSION):
        """baseuri: Base URI for the GenoLogics server, excluding
                    the 'api' or version parts!
                    For example: https://genologics.scilifelab.se:8443/
        username: The account name of the user to login as.
        password: The password for the user account to login as.
        version: The optional LIMS API version, by default 'v2' 
        """
        self.baseuri = baseuri.rstrip('/') + '/'
        self.username = username
        self.password = password
        self.VERSION = version
        self.cache = dict()
        self.cache_list = []
        # For optimization purposes, enables requests to persist connections
        self.request_session = requests.Session()
        # The connection pool has a default size of 10
        self.adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100)
        self.request_session.mount('http://', self.adapter)
        # Cache tube Container type, used in create_sample
        self.tube = None

    def get_uri(self, *segments, **query):
        "Return the full URI given the path segments and optional query."
        segments = ['api', self.VERSION] + list(segments)
        url = urljoin(self.baseuri, '/'.join(segments))
        if query:
            url += '?' + urlencode(query)
        return url

    def get(self, uri, params=dict()):
        "GET data from the URI. Return the response XML as an ElementTree."
        try:
            r = self.request_session.get(uri, params=params,
                                         auth=(self.username, self.password),
                                         headers=dict(accept='application/xml'),
                                         timeout=TIMEOUT)
        except requests.exceptions.Timeout as e:
            raise type(e)("{0}, Error trying to reach {1}".format(e.message, uri))

        else:
            return self.parse_response(r)

    def get_file_contents(self, id=None, uri=None):
        """Returns the contents of the file of <ID> or <uri>"""
        if id:
            segments = ['api', self.VERSION, 'files', id, 'download']
        elif uri:
            segments = [uri, 'download']
        else:
            raise ValueError("id or uri required")
        url = urljoin(self.baseuri, '/'.join(segments))
        r = self.request_session.get(url, auth=(self.username, self.password), timeout=TIMEOUT)
        self.validate_response(r)
        return r.text

    def upload_new_file(self, entity, file_to_upload):
        """Upload a file and attach it to the provided entity."""
        file_to_upload = os.path.abspath(file_to_upload)
        if not os.path.isfile(file_to_upload):
            raise IOError("{} not found".format(file_to_upload))

        # Request the storage space on glsstorage
        # Create the xml to describe the file
        root = ElementTree.Element(nsmap('file:file'))
        s = ElementTree.SubElement(root, 'attached-to')
        s.text = entity.uri
        s = ElementTree.SubElement(root, 'original-location')
        s.text = file_to_upload
        root = self.post(
                uri=self.get_uri('glsstorage'),
                data=self.tostring(ElementTree.ElementTree(root))
        )

        # Create the file object
        root = self.post(
                uri=self.get_uri('files'),
                data=self.tostring(ElementTree.ElementTree(root))
        )
        file = File(self, uri=root.attrib['uri'])

        # Actually upload the file
        uri = self.get_uri('files', file.id, 'upload')
        r = requests.post(uri, files={'file': (file_to_upload, open(file_to_upload, 'rb'))},
                          auth=(self.username, self.password))
        self.validate_response(r)
        return file

    def put(self, uri, data, params=dict()):
        """PUT the serialized XML to the given URI.
        Return the response XML as an ElementTree.
        """
        r = requests.put(uri, data=data, params=params,
                         auth=(self.username, self.password),
                         headers={'content-type': 'application/xml',
                                  'accept': 'application/xml'})
        return self.parse_response(r)

    def post(self, uri, data, params=dict()):
        """POST the serialized XML to the given URI.
        Return the response XML as an ElementTree.
        """
        r = requests.post(uri, data=data, params=params,
                          auth=(self.username, self.password),
                          headers={'content-type': 'application/xml',
                                   'accept': 'application/xml'})
        return self.parse_response(r, accept_status_codes=[200, 201, 202])

    def delete(self, uri):
        """Issue a HTTP DELETE request."""
        r = requests.delete(uri, auth=(self.username, self.password))
        if not r.status_code == 204:
            raise requests.exceptions.HTTPError(str(r.content))

    def check_version(self):
        """Raise ValueError if the version for this interface
        does not match any of the versions given for the API.
        """
        uri = urljoin(self.baseuri, 'api')
        r = requests.get(uri, auth=(self.username, self.password))
        root = self.parse_response(r)
        tag = nsmap('ver:versions')
        assert tag == root.tag
        for node in root.findall('version'):
            if node.attrib['major'] == self.VERSION: return
        raise ValueError('version mismatch')

    def validate_response(self, response, accept_status_codes=[200]):
        """Parse the XML returned in the response.
        Raise an HTTP error if the response status is not one of the
        specified accepted status codes.
        """
        if response.status_code not in accept_status_codes:
            try:
                root = ElementTree.fromstring(response.content)
                node = root.find('message')
                if node is None:
                    response.raise_for_status()
                    message = "%s" % (response.status_code)
                else:
                    message = "%s: %s" % (response.status_code, node.text)
                node = root.find('suggested-actions')
                if node is not None:
                    message += ' ' + node.text
            except ETREE_EXCEPTION: # some error messages might not follow the xml standard
                message=response.content 
            raise requests.exceptions.HTTPError(message)
        return True

    def parse_response(self, response, accept_status_codes=[200]):
        """Parse the XML returned in the response.
        Raise an HTTP error if the response status is not 200.
        """
        self.validate_response(response, accept_status_codes)
        root = ElementTree.fromstring(response.content)
        return root

    def get_udfs(self, name=None, attach_to_name=None, attach_to_category=None, start_index=None):
        """Get a list of udfs, filtered by keyword arguments.
        name: name of udf
        attach_to_name: item in the system, to wich the udf is attached, such as 
            Sample, Project, Container, or the name of a process.
        attach_to_category: If 'attach_to_name' is the name of a process, such as 'CaliperGX QC (DNA)',
             then you need to set attach_to_category='ProcessType'. Must not be provided otherwise.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name,
                                  attach_to_name=attach_to_name,
                                  attach_to_category=attach_to_category,
                                  start_index=start_index)
        return self._get_instances(Udfconfig, params=params)

    def get_reagent_types(self, name=None, start_index=None):
        """Get a list of reqgent types, filtered by keyword arguments.
        name: reagent type  name, or list of names.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name,
                                  start_index=start_index)
        return self._get_instances(ReagentType, params=params)

    def get_labs(self, name=None, last_modified=None,
                 udf=dict(), udtname=None, udt=dict(), start_index=None):
        """Get a list of labs, filtered by keyword arguments.
        name: Lab name, or list of names.
        last_modified: Since the given ISO format datetime.
        udf: dictionary of UDFs with 'UDFNAME[OPERATOR]' as keys.
        udtname: UDT name, or list of names.
        udt: dictionary of UDT UDFs with 'UDTNAME.UDFNAME[OPERATOR]' as keys
             and a string or list of strings as value.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name,
                                  last_modified=last_modified,
                                  start_index=start_index)
        params.update(self._get_params_udf(udf=udf, udtname=udtname, udt=udt))
        return self._get_instances(Lab, params=params)

    def get_researchers(self, firstname=None, lastname=None, username=None,
                        last_modified=None,
                        udf=dict(), udtname=None, udt=dict(), start_index=None):
        """Get a list of researchers, filtered by keyword arguments.
        firstname: Researcher first name, or list of names.
        lastname: Researcher last name, or list of names.
        username: Researcher account name, or list of names.
        last_modified: Since the given ISO format datetime.
        udf: dictionary of UDFs with 'UDFNAME[OPERATOR]' as keys.
        udtname: UDT name, or list of names.
        udt: dictionary of UDT UDFs with 'UDTNAME.UDFNAME[OPERATOR]' as keys
             and a string or list of strings as value.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(firstname=firstname,
                                  lastname=lastname,
                                  username=username,
                                  last_modified=last_modified,
                                  start_index=start_index)
        params.update(self._get_params_udf(udf=udf, udtname=udtname, udt=udt))
        return self._get_instances(Researcher, params=params)

    def get_projects(self, name=None, open_date=None, last_modified=None,
                     udf=dict(), udtname=None, udt=dict(), start_index=None):
        """Get a list of projects, filtered by keyword arguments.
        name: Project name, or list of names.
        open_date: Since the given ISO format date.
        last_modified: Since the given ISO format datetime.
        udf: dictionary of UDFs with 'UDFNAME[OPERATOR]' as keys.
        udtname: UDT name, or list of names.
        udt: dictionary of UDT UDFs with 'UDTNAME.UDFNAME[OPERATOR]' as keys
             and a string or list of strings as value.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name,
                                  open_date=open_date,
                                  last_modified=last_modified,
                                  start_index=start_index)
        params.update(self._get_params_udf(udf=udf, udtname=udtname, udt=udt))
        return self._get_instances(Project, params=params)

    def get_sample_number(self, name=None, projectname=None, projectlimsid=None,
                          udf=dict(), udtname=None, udt=dict(), start_index=None):
        """Gets the number of samples matching the query without fetching every
        sample, so it should be faster than len(get_samples()"""
        params = self._get_params(name=name,
                                  projectname=projectname,
                                  projectlimsid=projectlimsid,
                                  start_index=start_index)
        params.update(self._get_params_udf(udf=udf, udtname=udtname, udt=udt))
        root = self.get(self.get_uri(Sample._URI), params=params)
        total = 0
        while params.get('start-index') is None:  # Loop over all pages.
            total += len(root.findall("sample"))
            node = root.find('next-page')
            if node is None: break
            root = self.get(node.attrib['uri'], params=params)
        return total

    def get_samples(self, name=None, projectname=None, projectlimsid=None,
                    udf=dict(), udtname=None, udt=dict(), start_index=None):
        """Get a list of samples, filtered by keyword arguments.
        name: Sample name, or list of names.
        projectlimsid: Samples for the project of the given LIMS id.
        projectname: Samples for the project of the name.
        udf: dictionary of UDFs with 'UDFNAME[OPERATOR]' as keys.
        udtname: UDT name, or list of names.
        udt: dictionary of UDT UDFs with 'UDTNAME.UDFNAME[OPERATOR]' as keys
             and a string or list of strings as value.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name,
                                  projectname=projectname,
                                  projectlimsid=projectlimsid,
                                  start_index=start_index)
        params.update(self._get_params_udf(udf=udf, udtname=udtname, udt=udt))
        return self._get_instances(Sample, params=params)

    def get_artifacts(self, name=None, type=None, process_type=None,
                      artifact_flag_name=None, working_flag=None, qc_flag=None,
                      sample_name=None, samplelimsid=None, artifactgroup=None, containername=None,
                      containerlimsid=None, reagent_label=None,
                      udf=dict(), udtname=None, udt=dict(), start_index=None,
                      resolve=False):
        """Get a list of artifacts, filtered by keyword arguments.
        name: Artifact name, or list of names.
        type: Artifact type, or list of types.
        process_type: Produced by the process type, or list of types.
        artifact_flag_name: Tagged with the genealogy flag, or list of flags.
        working_flag: Having the given working flag; boolean.
        qc_flag: Having the given QC flag: UNKNOWN, PASSED, FAILED.
        sample_name: Related to the given sample name.
        samplelimsid: Related to the given sample id.
        artifactgroup: Belonging to the artifact group (experiment in client).
        containername: Residing in given container, by name, or list.
        containerlimsid: Residing in given container, by LIMS id, or list.
        reagent_label: having attached reagent labels.
        udf: dictionary of UDFs with 'UDFNAME[OPERATOR]' as keys.
        udtname: UDT name, or list of names.
        udt: dictionary of UDT UDFs with 'UDTNAME.UDFNAME[OPERATOR]' as keys
             and a string or list of strings as value.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name,
                                  type=type,
                                  process_type=process_type,
                                  artifact_flag_name=artifact_flag_name,
                                  working_flag=working_flag,
                                  qc_flag=qc_flag,
                                  sample_name=sample_name,
                                  samplelimsid=samplelimsid,
                                  artifactgroup=artifactgroup,
                                  containername=containername,
                                  containerlimsid=containerlimsid,
                                  reagent_label=reagent_label,
                                  start_index=start_index)
        params.update(self._get_params_udf(udf=udf, udtname=udtname, udt=udt))
        if resolve:
            return self.get_batch(self._get_instances(Artifact, params=params))
        else:
            return self._get_instances(Artifact, params=params)

    def get_containers(self, name=None, type=None,
                       state=None, last_modified=None,
                       udf=dict(), udtname=None, udt=dict(), start_index=None):
        """Get a list of containers, filtered by keyword arguments.
        name: Containers name, or list of names.
        type: Container type, or list of types.
        state: Container state: Empty, Populated, Discarded, Reagent-Only.
        last_modified: Since the given ISO format datetime.
        udf: dictionary of UDFs with 'UDFNAME[OPERATOR]' as keys.
        udtname: UDT name, or list of names.
        udt: dictionary of UDT UDFs with 'UDTNAME.UDFNAME[OPERATOR]' as keys
             and a string or list of strings as value.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name,
                                  type=type,
                                  state=state,
                                  last_modified=last_modified,
                                  start_index=start_index)
        params.update(self._get_params_udf(udf=udf, udtname=udtname, udt=udt))
        return self._get_instances(Container, params=params)

    def get_container_types(self, name):
        params = self._get_params(name=name)
        return self._get_instances(Containertype, params=params)

    def get_processes(self, last_modified=None, type=None,
                      inputartifactlimsid=None,
                      techfirstname=None, techlastname=None, projectname=None,
                      udf=dict(), udtname=None, udt=dict(), start_index=None):
        """Get a list of processes, filtered by keyword arguments.
        last_modified: Since the given ISO format datetime.
        type: Process type, or list of types.
        inputartifactlimsid: Input artifact LIMS id, or list of.
        udf: dictionary of UDFs with 'UDFNAME[OPERATOR]' as keys.
        udtname: UDT name, or list of names.
        udt: dictionary of UDT UDFs with 'UDTNAME.UDFNAME[OPERATOR]' as keys
             and a string or list of strings as value.
        techfirstname: First name of researcher, or list of.
        techlastname: Last name of researcher, or list of.
        projectname: Name of project, or list of.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(last_modified=last_modified,
                                  type=type,
                                  inputartifactlimsid=inputartifactlimsid,
                                  techfirstname=techfirstname,
                                  techlastname=techlastname,
                                  projectname=projectname,
                                  start_index=start_index)
        params.update(self._get_params_udf(udf=udf, udtname=udtname, udt=udt))
        return self._get_instances(Process, params=params)

    def get_process_types(self, displayname=None):
        """Get a list of process types with the specified name."""
        params = self._get_params(displayname=displayname)
        return self._get_instances(Processtype, params=params)

    def get_reagent_types(self, name=None):
        params = self._get_params(name=name)
        return self._get_instances(ReagentType, params=params)

    def get_reagent_kits(self, name=None):
        params = self._get_params(name=name)
        return self._get_instances(ReagentKit, params=params)

    def get_reagent_lots(self, name=None, kitname=None, number=None):
        params = self._get_params(name=name, kitname=kitname, number=number)
        return self._get_instances(ReagentLot, params=params)

    def get_workflows(self, name=None):
        params = self._get_params(name=name)
        return self._get_instances(Workflow, params=params)

    def get_protocols(self, name=None):
        """Get a list of protocol configuration entities.
        Optionally filter by protocol name."""
        params = self._get_params(name=name)
        return self._get_instances(ProtocolConfiguration, params=params)

    def get_reagent_kits(self, name=None, start_index=None):
        """Get a list of reagent kits, filtered by keyword arguments.
        name: reagent kit  name, or list of names.
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name,
                                  start_index=start_index)
        return self._get_instances(ReagentKit, params=params)

    def get_reagent_lots(self, name=None, kitname=None, number=None,
                         start_index=None):
        """Get a list of reagent lots, filtered by keyword arguments.
        name: reagent kit  name, or list of names.
        kitname: name of the kit this lots belong to
        number: lot number or list of lot number
        start_index: Page to retrieve; all if None.
        """
        params = self._get_params(name=name, kitname=kitname, number=number,
                                  start_index=start_index)
        return self._get_instances(ReagentLot, params=params)

    def _get_params(self, **kwargs):
        "Convert keyword arguments to a kwargs dictionary."
        result = dict()
        for key, value in kwargs.items():
            if value is None: continue
            result[key.replace('_', '-')] = value
        return result

    def _get_params_udf(self, udf=dict(), udtname=None, udt=dict()):
        "Convert UDF-ish arguments to a params dictionary."
        result = dict()
        for key, value in udf.items():
            result["udf.%s" % key] = value
        if udtname is not None:
            result['udt.name'] = udtname
        for key, value in udt.items():
            result["udt.%s" % key] = value
        return result

    def _get_instances(self, klass, params=dict()):
        result = []
        tag = klass._TAG
        if tag is None:
            tag = klass.__name__.lower()
        root = self.get(self.get_uri(klass._URI), params=params)
        while params.get('start-index') is None:  # Loop over all pages.
            for node in root.findall(tag):
                result.append(klass(self, uri=node.attrib['uri']))
            node = root.find('next-page')
            if node is None: break
            root = self.get(node.attrib['uri'], params=params)
        return result

    def get_batch(self, instances, force=False):
        """Get the content of a set of instances using the efficient batch call.

        Returns the list of requested instances in arbitrary order, with duplicates removed
        (duplicates=entities occurring more than once in the instances argument).

        For Artifacts it is possible to have multiple instances with the same LIMSID but
        different URI, differing by a query parameter ?state=XX. If state is not
        given for an input URI, a state is added in the data returned by the batch
        API. In this case, the URI of the Entity object is not updated by this function
        (this is similar to how Entity.get() works). This may help with caching.

        The batch request API call collapses all requested Artifacts with different
        state into a single result with state equal to the state of the Artifact
        occurring at the last position in the list.
        """
        if not instances:
            return []
        root = ElementTree.Element(nsmap('ri:links'))
        needs_request = False
        instance_map = {}
        for instance in instances:
            instance_map[instance.id] = instance
            if force or instance.root is None:
                ElementTree.SubElement(root, 'link', dict(uri=instance.uri,
                                                          rel=instance.__class__._URI))
                needs_request = True

        if needs_request:
            uri = self.get_uri(instance.__class__._URI, 'batch/retrieve')
            data = self.tostring(ElementTree.ElementTree(root))
            root = self.post(uri, data)
            for node in root.getchildren():
                instance = instance_map[node.attrib['limsid']]
                instance.root = node
        return instance_map.values()

    def put_batch(self, instances):
        """Update multiple instances using a single batch request."""

        if not instances:
            return

        root = None  # XML root element for batch request

        for instance in instances:
            if root is None:
                klass = instance.__class__
                # Tag is art:details, con:details, etc.
                example_root = instance.root
                ns_uri = re.match("{(.*)}.*", example_root.tag).group(1)
                root = ElementTree.Element("{%s}details" % (ns_uri))

            root.append(instance.root)

        uri = self.get_uri(klass._URI, 'batch/update')
        data = self.tostring(ElementTree.ElementTree(root))
        root = self.post(uri, data)

    def route_artifacts(self, artifact_list, workflow_uri=None, stage_uri=None, unassign=False):
        root = ElementTree.Element(nsmap('rt:routing'))
        if unassign:
            s = ElementTree.SubElement(root, 'unassign')
        else:
            s = ElementTree.SubElement(root, 'assign')
        if workflow_uri:
            s.set('workflow-uri', workflow_uri)
        if stage_uri:
            s.set('stage-uri', stage_uri)
        for artifact in artifact_list:
            a = ElementTree.SubElement(s, 'artifact')
            a.set('uri', artifact.uri)

        uri = self.get_uri('route', 'artifacts')
        r = requests.post(uri, data=self.tostring(ElementTree.ElementTree(root)),
                          auth=(self.username, self.password),
                          headers={'content-type': 'application/xml',
                                   'accept': 'application/xml'})
        self.validate_response(r)

    def tostring(self, etree):
        "Return the ElementTree contents as a UTF-8 encoded XML string."
        outfile = BytesIO()
        self.write(outfile, etree)
        return outfile.getvalue()

    def write(self, outfile, etree):
        "Write the ElementTree contents as UTF-8 encoded XML to the open file."
        
        # TODO: Work-around for charset problems in API. To be reverted if we can ever submit raw
        # UTF-8 data again.
        # (Since this is a temporary measure, we're just doing it easy and making *another* BytesIO
        # buffer, even though this will normally be called via tostring, which also makes a BytesIO)
        tempfile = BytesIO()
        etree.write(tempfile, encoding='utf-8') #Python 2.6 compat: don't specify xml_declaration arg

        replace = {
                u'æ': u'a',
                u'Æ': u'A',
                u'ø': u'o',
                u'Ø': u'O',
                u'å': u'a',
                u'Å': u'A'
                }
        req = tempfile.getvalue().decode('utf-8')
        for pat,repl in replace.items():
            req = req.replace(pat, repl)
        outfile.write(req.encode('utf-8'))

    def create_step(self, step_configuration, inputs):
        """Creates a new protocol step instance. The inputs parameter is a list of 
		artifact inputs. Returns the new step."""
		
        root = ElementTree.Element('stp:step-creation', {'xmlns:stp': 'http://genologics.com/ri/step'})
        ElementTree.SubElement(root, "configuration", {'uri': step_configuration.uri})
        inputs_element = ElementTree.SubElement(root, "inputs")
        for i in inputs:
            ElementTree.SubElement(inputs_element, "input", {'uri': i.uri})

        root = self.post(self.get_uri("steps"), ElementTree.tostring(root))
        limsid = root.attrib.get('limsid')
        step = Step(self, id = limsid)
        step.root = root
        return step

    def create_lot(self, reagent_kit, name, lot_number=None, expiry_date=None,
            storage_location=None, notes=None, status=None):
        root = ElementTree.Element("lot:reagent-lot", {"xmlns:lot": "http://genologics.com/ri/reagentlot"})
        ElementTree.SubElement(root, 'reagent-kit', {'uri': reagent_kit.uri})
        ElementTree.SubElement(root, 'name').text = name
        if lot_number:
            ElementTree.SubElement(root, 'lot-number').text = lot_number
        if expiry_date:
            ElementTree.SubElement(root, 'expiry-date').text = expiry_date
        if storage_location:
            ElementTree.SubElement(root, 'storage_location').text = storage_location
        if notes:
            ElementTree.SubElement(root, 'notes').text = notes
        if status:
            ElementTree.SubElement(root, 'status').text = status
        xml_data = self.tostring(ElementTree.ElementTree(root))
        response = self.post(self.get_uri("reagentlots"), xml_data)
        lot = ReagentLot(self, uri=response.attrib['uri'])
        lot.root = response
        return lot

    def create_project(self, name, researcher, open_date=None, udf={}):
        """Create a project, specifying only the required information.

        Returns a new Project object."""
        root = ElementTree.Element('prj:project', {'xmlns:prj': 'http://genologics.com/ri/project'})
        ElementTree.SubElement(root, 'name').text = name
        ElementTree.SubElement(root, 'researcher', {'uri': researcher.uri})
        for k, v in udf.items():
            #proj.udf[k] = v
            # TODO
            pass
        if open_date:
            ElementTree.SubElement(root, 'open-date', str(open_date))
        xml_data = self.tostring(ElementTree.ElementTree(root))
        response = self.post(self.get_uri("projects"), xml_data)
        project = Project(self, uri=response.attrib['uri'])
        project.root = reponse
        return response

    def create_container(self, type, name=None):
        root = ElementTree.Element('con:container', {'xmlns:con': 'http://genologics.com/ri/container'})
        ElementTree.SubElement(root, 'type', {'uri': type.uri})
        if name:
            ElementTree.SubElement(root, 'name').text = name
        xml_data = self.tostring(ElementTree.ElementTree(root))
        response = self.post(self.get_uri("containers"), xml_data)
        container = Container(self, uri=response.attrib['uri'])
        container.root = response
        return container

    def create_sample(self, name, project, container=None, well=None, udf={}):
        """Create a sample.  Returns a new Sample object."""
        root = ElementTree.Element('smp:samplecreation', {'xmlns:smp': 'http://genologics.com/ri/sample'})
        ElementTree.SubElement(root, 'name').text = name
        ElementTree.SubElement(root, 'project', {'uri': project.uri})
        create_container = container is None
        if create_container:
            if not self.tube:
                self.tube = self.get_container_types('Tube')[0]
            container = self.create_container(self.tube)
            print (container)
            well = '1:1'
        location = ElementTree.SubElement(root, 'location')
        ElementTree.SubElement(location, 'container', {'uri': container.uri})
        ElementTree.SubElement(location, 'value').text = well
        for k, v in udf.items():
            # TODO
            pass
        xml_data = self.tostring(ElementTree.ElementTree(root))
        try:
            response = self.post(self.get_uri("samples"), xml_data)
        except requests.exceptions.HTTPError:
            if create_container:
                container.delete()
            raise
        sample = Sample(self, uri=response.attrib['uri'])
        sample.root = response
        return sample

    def glsstorage(self, attached_to, original_location):
        """Allocates and returns a file resource in the glsstorage area. This 
        doesn't actually upload the file, it only sets up the metadata.

        attached_to should be an Entity, and original_location should be a string
        
        Returns a Glsstorage object, which contains information about
        a file or storage location, but does not yet have a LIMS ID. The POST 
        request done here fills in the content-location attribute."""

        proto_file = Glsstorage(self)
        proto_file.attached_to_uri = attached_to.uri
        proto_file.original_location = original_location
        xml_data = self.tostring(ElementTree.ElementTree(proto_file.root))
        glss_uri = self.get_uri("glsstorage")
        response = self.post(glss_uri, xml_data)
        return Glsstorage(self, root=response)
        
    def route_analytes(self, analytes, target):
        """Adding analytes to workflow or stage (target)."""

        root = ElementTree.Element('rt:routing', {'xmlns:rt': 'http://genologics.com/ri/routing'})
        if isinstance(target, Workflow):
            assign = ElementTree.SubElement(root, "assign", {'workflow-uri': target.uri})
        elif isinstance(target, Stage):
            assign = ElementTree.SubElement(root, "assign", {'stage-uri': target.uri})
        else:
            raise ValueError("target parameter must be a Workflow or Stage")

        do_request = False
        for i in analytes:
            ElementTree.SubElement(assign, "artifact", {'uri': i.uri})
            do_request = True

        if do_request:
            self.post(self.get_uri("route", "artifacts"), ElementTree.tostring(root))

    def set_default_next_step(self, step, analytes):
        """Assign analytes to default next step.

        Utilitiy function which implements a common use case."""

        if step.configuration.transitions:
            next_step_uri = step.configuration.transitions[0].get("next-step-uri")
            action = "nextstep"
        else:
            action = "complete"

        for next_action in step.actions.next_actions:
            if action == "nextstep":
                next_action['step-uri'] = next_step_uri
            next_action['action'] = action

        step.actions.put()

    def get_qc_results(self, analytes, qc_process_name):
        """Get QC results for a list of analytes, from a process which produces 
        ResultFiles, which had the specified analytes directly as inputs.

        qc_process_name: The name of the QC process to get results from.

        Returns the QC results (ResultFile artifacts) in the same order as
        the input list of analytes.

        Raises a KeyError if any sample does not have a QC result file.
        """

        qc_processes = self.get_processes(
                inputartifactlimsid=[a.id for a in analytes],
                type=qc_process_name
                )

        qc_results = {}
        # Uses most recent QC result for each sample
        for qc_process in sorted(qc_processes, key=lambda x: x.date_run):
            for i, o in qc_process.input_output_maps:
                if o and o['output-type'] == "ResultFile" and o['output-generation-type'] == 'PerInput':
                    qc_results[i['uri'].id] = o['uri']

        return [qc_results[a.id] for a in analytes]


