import logging
import uuid
from threading import Lock
from hashlib import md5
from xml.parsers.expat import ExpatError

from plone.alterego import dynamic
from plone.alterego.interfaces import IDynamicObjectFactory
from plone.supermodel import serializeSchema, loadString
from plone.schemaeditor.interfaces import ISchemaContext
from plone.synchronize import synchronized
from zope.component import queryUtility
from zope.interface import implements
from zope.interface.interfaces import IInterface
from zope.interface.declarations import Implements
from zope.interface.declarations import implementedBy
from zope.interface.declarations import ObjectSpecificationDescriptor
from zope.interface.declarations import getObjectSpecification
from zope.schema import getFieldNamesInOrder
from BTrees.OOBTree import OOBTree

from uu.record.base import Record

from uu.dynamicschema.interfaces import ISchemaSaver
from uu.dynamicschema.interfaces import ISchemaSignedEntity
from uu.dynamicschema.interfaces import DEFAULT_MODEL_XML, DEFAULT_SIGNATURE


logger = logging.getLogger('uu.dynamicschema')

generated = dynamic.create('uu.dynamicschema.schema.generated')

#empty schema loader using policy defined above:
new_schema = lambda: loadString(DEFAULT_MODEL_XML).schema

loaded = {} # cached signatures to transient schema objects

def parse_schema(xml):
    if not xml.strip():
        return new_schema()
    try:
        return loadString(xml).schema
    except ExpatError:
        raise RuntimeError('could not parse field schema xml')


class SchemaSaver(OOBTree):
    """
    Mapping to persist xml schema from plone.supermodel. Values are
    xml stripped of trailing/leading whitespace, keys are md5 hexidecimal
    digest signatures of the XML serialization of the schema.
    """
    
    implements(ISchemaSaver)
    
    def __init__(self):
        super(SchemaSaver, self).__setitem__(DEFAULT_SIGNATURE,
                                             DEFAULT_MODEL_XML)
    
    def signature(self, schema):
        if IInterface.providedBy(schema):
            schema = serializeSchema(schema)
        return md5(schema.strip()).hexdigest()
    
    def add(self, schema):
        """
        given schema as xml or interface, save to mapping, return md5
        signature for the saved xml serialization.
        """
        if IInterface.providedBy(schema):
            xml = serializeSchema(schema).strip()
            signature = self.signature(xml)
            if signature != DEFAULT_SIGNATURE:
                self.invalidate(schema) # if schema modified, del stale sig
                loaded[signature] = schema
        else:
            xml = schema.strip()
            signature = self.signature(xml)
        self[signature] = xml
        return signature
    
    def __setitem__(self, key, value):
        if key is DEFAULT_SIGNATURE:
            raise KeyError('Default schema cannot be modified')
        value = str(value).strip()
        if self.signature(value) != key:
            raise ValueError('key does not match signature of value')
        super(SchemaSaver, self).__setitem__(key, value)
    
    def __delitem__(self, key):
        if key is DEFAULT_SIGNATURE:
            raise KeyError('Default schema cannot be removed')
        super(SchemaSaver, self).__delitem__(key, value)
    
    def load(self, xml):
        global loaded
        if xml.strip() == DEFAULT_MODEL_XML:
            return new_schema()
        signature = self.signature(xml)
        if signature not in loaded:
            loaded[signature] = parse_schema(xml)
        return loaded[signature]
    
    def invalidate(self, schema):
        """invalidate transient cached/loaded interface/schema object"""
        global loaded
        delkey = None
        for k,v in loaded.items():
            if v is schema:
                delkey = k
        if delkey:
            del(loaded[delkey])


class SignatureSchemaFactory(object):
    """
    Factory for runtime dynamic interfaces based on md5 signatures as a
    deterministic, reliable way to name interfaces saved in a 
    plone.alterego dynamic module.
    
    Objects providing these interfaces should use SignatureAwareDescriptor
    (below) to implement a __providedBy__ descriptor for dynamic lookup of
    provided interfaces.  In turn, that descriptor will consult a dynamic
    module using this factory-by-name to always get the same interface
    and schema keyed by the signature.
    
    The naming convention for interfaces between this factory and the
    SignatureAwareDescriptor is ('I%s' % signature).
    """
    
    implements(IDynamicObjectFactory)
    
    _lock = Lock()
    
    @synchronized(_lock)
    def __call__(self, name, module):
        global loaded
        # use schema-saver to get interface
        signature = name[1:] # "I[md5hex]" -> "[md5hex]"
        if signature in loaded:
            return loaded[signature]
        saver = queryUtility(ISchemaSaver)
        if signature in saver:
            schema = saver.load(saver.get(signature)) #schema/iface object
            loaded[signature] = schema
        else:
            # otherwise load a placeholder interface
            logger.warning('SignatureSchemaFactory: '\
                           'Unable to obtain dynamic schema from '\
                           'serialization; using placeholder.')
            schema = InterfaceClass(
                name,
                (interface.Interface),
                __module__=module.__name__,
                ) #placeholder (anonymous marker) interface
        return schema


class SignatureAwareDescriptor(ObjectSpecificationDescriptor):
    """Descriptor for dynamic __providedBy__ on schema signed objects"""
    
    def __get__(self, inst, cls=None):
        global generated
        if inst is None:
            return getObjectSpecification(cls)
        spec = directly_provided = getattr(inst, '__provides__', None)
        if spec is None:
            spec = implementedBy(cls)
        signature = getattr(inst, 'signature', None)
        if signature is None:
            return spec
        iface_name =  'I%s' % signature
        dynamic = [getattr(generated, iface_name)]
        dynamic.append(spec)
        spec = Implements(*dynamic)
        return spec


class SignatureSchemaContext(object):
    implements(ISchemaContext)
    
    signature = DEFAULT_SIGNATURE
    
    def __init__(self, signature=None):
        self.signature = signature
    
    @property
    def schema(self):
        signature = self.signature
        if signature is None:
            signature = self.signature = self.__class__.signature
        if not hasattr(self, '_v_schema') or self._v_schema[0] != signature:
            # non-existent or stale _v_schema attribute; a change in
            # peristent signature will invalidate _v_schema
            saver = queryUtility(ISchemaSaver)
            self._v_schema = (
                signature,
                saver.load(saver.get(signature, None) or DEFAULT_MODEL_XML),
                )
        return self._v_schema[1]


class SchemaSignedEntity(Record, SignatureSchemaContext):
    """
    Base class for schema-signed entity.
    """
    
    implements(ISchemaSignedEntity)
    
    signature = None #instances should override this via sign()
    
    __providedBy__ = SignatureAwareDescriptor()
    
    def __init__(self, context=None, record_uid=None):
        self.context = self.__parent__ = context
        self.record_uid = record_uid or str(uuid.uuid4()) #random
        Record.__init__(self, context, record_uid)
        SignatureSchemaContext.__init__(self, signature=None)
        if getattr(context, 'schema', None):
            self.sign(context.schema)
    
    def __getattr__(self, name):
        """If field, return default for attribute value"""
        if name.startswith('_v_'):
            raise AttributeError(name) # no magic tricks with these.
        schema = self.__class__.schema.__get__(self) #aq property workaround!
        if name == 'schema':
            return schema
        if schema is not None:
            fieldnames = getFieldNamesInOrder(schema)
            if name in fieldnames:
                field = schema.get(name)
                return field.default
        raise AttributeError(name)
    
    def sign(self, schema):
        """
        sign the object with the signature of the schema used on it
        """
        saver = queryUtility(ISchemaSaver)
        #persist serialization of schema, get signature
        self.signature = saver.add(schema)
