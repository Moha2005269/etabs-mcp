// Dumps the public surface of the CSI ETABS API assembly (ETABSv1.dll) to JSON.
// The output drives the MCP server's generic, signature-validated API dispatcher.
//
// Usage: dotnet run -- "<path to ETABSv1.dll>" "<output json>"
using System.Reflection;
using System.Text.Json;

var dllPath = args.Length > 0 ? args[0] : @"C:\Program Files\Computers and Structures\ETABS 23\ETABSv1.dll";
var outPath = args.Length > 1 ? args[1] : "etabs_api_spec.json";

var asm = Assembly.LoadFrom(dllPath);
Type[] types;
try { types = asm.GetExportedTypes(); }
catch (ReflectionTypeLoadException e) { types = e.Types.Where(t => t != null).ToArray()!; }

static string TypeName(Type t)
{
    if (t.IsByRef) t = t.GetElementType()!;
    if (t.IsArray) return TypeName(t.GetElementType()!) + "[]";
    if (t.IsGenericType)
        return t.Name.Split('`')[0] + "<" + string.Join(",", t.GetGenericArguments().Select(TypeName)) + ">";
    return t.Namespace == "System" ? t.Name : t.Name;
}

var interfaces = new SortedDictionary<string, object>();
var enums = new SortedDictionary<string, object>();
var classes = new SortedDictionary<string, object>();

foreach (var t in types.OrderBy(t => t.FullName))
{
    if (t.IsEnum)
    {
        var values = new SortedDictionary<string, long>();
        foreach (var n in Enum.GetNames(t))
            values[n] = Convert.ToInt64(Enum.Parse(t, n));
        enums[t.Name] = values;
        continue;
    }

    var flags = BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly;
    var props = t.GetProperties(flags)
        .Select(p => new { name = p.Name, type = TypeName(p.PropertyType), can_write = p.CanWrite })
        .OrderBy(p => p.name).ToList();
    var propAccessors = new HashSet<MethodInfo>(t.GetProperties(flags).SelectMany(p => p.GetAccessors()));

    var methods = t.GetMethods(flags)
        .Where(m => !propAccessors.Contains(m) && !m.IsSpecialName)
        .OrderBy(m => m.Name).ThenBy(m => m.GetParameters().Length)
        .Select(m => new
        {
            name = m.Name,
            returns = TypeName(m.ReturnType),
            @params = m.GetParameters().Select(p => new
            {
                name = p.Name,
                type = TypeName(p.ParameterType),
                by_ref = p.ParameterType.IsByRef,
                @out = p.IsOut,
                optional = p.IsOptional,
                @default = p.HasDefaultValue && p.DefaultValue != null && p.DefaultValue != DBNull.Value
                    ? p.DefaultValue.ToString() : null,
                is_enum = (p.ParameterType.IsByRef ? p.ParameterType.GetElementType()! : p.ParameterType) is var et
                    && (et.IsArray ? et.GetElementType()! : et).IsEnum,
            }).ToList(),
        }).ToList();

    // Interfaces only list what they declare; inherited members are resolved through "extends".
    var extends = t.GetInterfaces().Where(i => i.Assembly == asm).Select(i => i.Name).OrderBy(n => n).ToList();
    var entry = new { kind = t.IsInterface ? "interface" : "class", extends, properties = props, methods };
    if (t.IsInterface) interfaces[t.Name] = entry; else classes[t.Name] = entry;
}

var spec = new
{
    source = Path.GetFileName(dllPath),
    assembly_version = asm.GetName().Version?.ToString(),
    file_version = System.Diagnostics.FileVersionInfo.GetVersionInfo(dllPath).FileVersion,
    generated_utc = DateTime.UtcNow.ToString("o"),
    interfaces,
    classes,
    enums,
};

File.WriteAllText(outPath, JsonSerializer.Serialize(spec, new JsonSerializerOptions { WriteIndented = true }));
Console.WriteLine($"interfaces={interfaces.Count} classes={classes.Count} enums={enums.Count} " +
                  $"methods={interfaces.Values.Concat(classes.Values).Sum(e => ((dynamic)e).methods.Count)} -> {outPath}");
