using System;

// Mono on the fixture-generation host does not include this framework
// namespace. These attributes provide the API surface ObjFmt reflects on.
namespace System.Runtime.Serialization {
  [AttributeUsage(AttributeTargets.Class | AttributeTargets.Struct)]
  public sealed class DataContractAttribute : Attribute { }

  [AttributeUsage(AttributeTargets.Field | AttributeTargets.Property)]
  public sealed class DataMemberAttribute : Attribute {
    public int Order { get; set; }
  }

  [AttributeUsage(AttributeTargets.Class, AllowMultiple = true)]
  public sealed class KnownTypeAttribute : Attribute {
    public KnownTypeAttribute(Type type) { }
  }
}
